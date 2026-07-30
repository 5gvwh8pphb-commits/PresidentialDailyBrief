#!/usr/bin/env python3
"""Render a brief JSON file as an HTML email body.

Deterministic - no model involved, so it costs nothing and cannot drift.

    python tools/brief_email.py --json district/2026-08-02.json \
        --title "My District News" \
        --url https://example.github.io/repo/district/ \
        --out email.html --subject-out subject.txt
"""
import argparse
import html
import json
import io

KIND_LABEL = {
    "admin": "Administration",
    "trust": "Trust",
    "other": "District news",
    "student": "Students",
}
KIND_COLOUR = {
    "admin": "#B4741A",
    "trust": "#5F55C4",
    "other": "#1D6FC0",
    "student": "#12805E",
}
FONT = "Arial, Helvetica, sans-serif"


def esc(v):
    return html.escape("" if v is None else str(v))


def row(story):
    kind = story.get("kind") if story.get("kind") in KIND_LABEL else "other"
    colour = KIND_COLOUR[kind]
    label = KIND_LABEL[kind]
    head = esc(story.get("headline", ""))
    url = story.get("url")
    if url:
        head = (
            '<a href="%s" style="color:#11324a;text-decoration:underline">%s</a>'
            % (esc(url), head)
        )
    src = esc(story.get("source") or "")
    return """
      <tr>
        <td style="padding:0 0 14px 0">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"
                 style="border:1px solid #d8dee6;border-left:4px solid %s">
            <tr>
              <td style="padding:12px 14px;font-family:%s">
                <div style="font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:%s">
                  %s &nbsp;&middot;&nbsp; %s
                </div>
                <div style="font-size:16px;font-weight:bold;color:#11324a;padding:5px 0 6px 0;line-height:1.3">
                  %s
                </div>
                <div style="font-size:14px;color:#40505f;line-height:1.55">%s</div>
                %s
              </td>
            </tr>
          </table>
        </td>
      </tr>""" % (
        colour,
        FONT,
        colour,
        esc(story.get("district", "")),
        esc(story.get("age", "")),
        head,
        esc(story.get("body", "")),
        (
            '<div style="font-size:11px;color:#8894a2;padding-top:7px">%s &mdash; %s</div>'
            % (label, src)
            if src
            else '<div style="font-size:11px;color:#8894a2;padding-top:7px">%s</div>' % label
        ),
    )


def build(data, title, url):
    stories = sorted(data.get("stories", []), key=lambda s: s.get("n") or 0)
    date_label = esc(data.get("dateLabel") or data.get("date") or "")
    window = esc(data.get("window") or "")
    note = data.get("note")

    if stories:
        body = "".join(row(s) for s in stories)
        count = len(stories)
        admin = sum(1 for s in stories if s.get("kind") == "admin")
        summary = "%d %s, %d on administration." % (
            count, "story" if count == 1 else "stories", admin
        )
    else:
        body = """
      <tr>
        <td style="padding:22px 16px;border:1px dashed #c8d0da;text-align:center;
                   font-family:%s;font-size:14px;color:#5d6b7d">
          Nothing qualifying surfaced this time. Reported rather than padded.
        </td>
      </tr>""" % FONT
        summary = "No qualifying stories this time."

    note_block = ""
    if note:
        note_block = """
      <tr>
        <td style="padding:0 0 16px 0;font-family:%s;font-size:13.5px;color:#40505f;
                   line-height:1.55;background:#f4f6f9;border-left:4px solid #B4741A;
                   padding:11px 13px">%s</td>
      </tr>""" % (FONT, esc(note))

    covered = data.get("covered") or []
    covered_block = ""
    if covered:
        covered_block = """
      <tr>
        <td style="padding:6px 0 0 0;font-family:%s;font-size:11px;color:#8894a2;line-height:1.7">
          Searched: %s
        </td>
      </tr>""" % (FONT, esc(" · ".join(covered)))

    return """<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#eef1f5">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%" style="background:#eef1f5">
  <tr><td align="center" style="padding:22px 12px">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600"
           style="width:600px;max-width:600px;background:#ffffff;border:1px solid #d8dee6">
      <tr>
        <td style="padding:18px 20px;border-bottom:3px solid #B4741A;font-family:%s">
          <div style="font-size:19px;font-weight:bold;color:#11324a;letter-spacing:.02em">%s</div>
          <div style="font-size:12px;color:#5d6b7d;padding-top:4px">%s &nbsp;&middot;&nbsp; %s</div>
        </td>
      </tr>
      <tr><td style="padding:18px 20px">
        <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%">
          <tr><td style="font-family:%s;font-size:13px;color:#5d6b7d;padding:0 0 14px 0">%s</td></tr>
          %s
          %s
          %s
        </table>
      </td></tr>
      <tr>
        <td style="padding:14px 20px;border-top:1px solid #e4e9ef;font-family:%s;
                   font-size:12px;color:#5d6b7d">
          <a href="%s" style="color:#B4741A;text-decoration:none;font-weight:bold">Open the full brief &rarr;</a>
          <div style="padding-top:6px;color:#8894a2">Generated automatically. Reply to nobody; this mailbox is unattended.</div>
        </td>
      </tr>
    </table>
  </td></tr>
</table>
</body></html>""" % (
        FONT, esc(title), date_label, window,
        FONT, esc(summary), note_block, body, covered_block,
        FONT, esc(url),
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--subject-out")
    a = p.parse_args()

    with io.open(a.json, encoding="utf-8") as fh:
        data = json.load(fh)

    with io.open(a.out, "w", encoding="utf-8") as fh:
        fh.write(build(data, a.title, a.url))

    if a.subject_out:
        stories = data.get("stories", [])
        n = len(stories)
        if n:
            tail = "%d %s" % (n, "story" if n == 1 else "stories")
        else:
            tail = "nothing this time"
        subject = "%s — %s (%s)" % (
            a.title, data.get("dateLabel") or data.get("date"), tail
        )
        with io.open(a.subject_out, "w", encoding="utf-8") as fh:
            fh.write(subject)

    print("wrote %s (%d stories)" % (a.out, len(data.get("stories", []))))


if __name__ == "__main__":
    main()
