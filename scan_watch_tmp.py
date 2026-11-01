import json

names = ['Duneland','Portage','Highland','Lake Station','Hammond','Whiting','Gary','Lake Ridge']
base = '/home/runner/work/PresidentialDailyBrief/PresidentialDailyBrief/watch/'
files = ['2026-10-04.json','2026-10-05.json','2026-10-11.json','2026-10-18.json','2026-10-25.json']
for f in files:
    d = json.load(open(base + f))
    print('=== FILE:', f, '===')
    for key in d.keys():
        val = d[key]
        if isinstance(val, list):
            for it in val:
                s = json.dumps(it)
                if any(name in s for name in names):
                    print(key, json.dumps(it))
