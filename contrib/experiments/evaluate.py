"""Compare the ordinal matcher (anchor_ordinal.py, from commit 0c54330) with the
current boundary matcher across every paired book, using chapter numbers as a
(flawed, lower-bound) ground truth. Read-only. See docs/design.md.
"""
import sqlite3,json,re,zipfile,html,collections
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..", "..")))
sys.path.insert(0, HERE)
from concordance.calibre import load_books,find_epub,read_spine
from concordance.absclient import Chapter,AbsBook
from concordance.matching import normalise_title as norm
import concordance.anchor as new
import anchor_ordinal as old

WORDS={w:i for i,w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
def number(s):
    s=(s or "").lower()
    m=re.search(r"\b(?:chapter|ch\.?|part)?\s*(\d{1,3})\b",s)
    if m: return int(m.group(1))
    m=re.search(r"\bchapter\s+("+"|".join(WORDS)+r")\b",s)
    return WORDS[m.group(1)] if m else None

def heading_numbers(epub, spine):
    z=zipfile.ZipFile(epub); names={n.split("/")[-1]:n for n in z.namelist()}; out={}
    for it in spine:
        try: raw=z.read(names[it.href.split("/")[-1]]).decode("utf8","ignore")
        except KeyError: continue
        hs=[re.sub(r"\s+"," ",html.unescape(re.sub(r"<[^>]+>"," ",h))).strip() for h in re.findall(r"(?s)<h[1-3][^>]*>(.*?)</h[1-3]>",raw)]
        n=next((number(h) for h in hs if number(h) is not None),None)
        if n is not None: out[it.index]=n
    return out

def accuracy(al, chapters, headnum):
    """Share of numbered spine items placed in an audio chapter carrying the same number."""
    num={c.index:number(c.title) for c in chapters}
    hits=tot=0
    for p in al.points:
        if p.spine_index not in headnum: continue
        c=next((c for c in chapters if c.start <= p.audio_start + 1 < c.end), None)
        if c is None or num.get(c.index) is None: continue
        tot+=1; hits+= int(num[c.index]==headnum[p.spine_index])
    return hits,tot

ROOT=os.environ["CALIBRE_ROOT"].rstrip("/")  # Calibre library directory
books=load_books(os.environ.get("CALIBRE_DB") or f"{ROOT}/metadata.db",ROOT)
byn={}
for b in books.values(): byn.setdefault(norm(b.title),b)
a=sqlite3.connect(f"file:{os.environ['ABS_DB']}?mode=ro",uri=True)  # ABS config/absdatabase.sqlite
inprog={r[0] for r in a.execute("SELECT mediaItemId FROM mediaProgresses WHERE currentTime>0 AND isFinished=0")}
conf_old=collections.Counter(); conf_new=collections.Counter(); acc=collections.Counter(); rows=[]
for bid,t,ch,du in a.execute("SELECT b.id,b.title,b.chapters,b.duration FROM books b JOIN libraryItems li ON li.mediaId=b.id WHERE li.mediaType='book'"):
    cb=byn.get(norm(t)); ep=find_epub(cb) if cb else None
    if not ep: continue
    try: sp=read_spine(ep)
    except RuntimeError: continue
    chs=AbsBook("x",t,"",du or 0,[Chapter(i,c.get("title",""),float(c["start"]),float(c["end"])) for i,c in enumerate(json.loads(ch or "[]"),1)]).substantive_chapters()
    ao=old.build_alignment(sp,chs); an=new.build_alignment(sp,chs)
    conf_old[ao.confidence]+=1; conf_new[an.confidence]+=1
    hn=heading_numbers(ep,[s for s in sp if s.is_content])
    ho,to=accuracy(ao,chs,hn); hnw,tn=accuracy(an,chs,hn)
    if to>=5 and tn>=5:
        acc["books"]+=1; acc["old_hits"]+=ho; acc["old_tot"]+=to; acc["new_hits"]+=hnw; acc["new_tot"]+=tn
        if ho/to < 0.9 or hnw/tn < 0.9: rows.append((t,ho,to,hnw,tn,ao.confidence,an.confidence))
    if bid in inprog:
        rows.append((t+" *",ho,to,hnw,tn,ao.confidence,an.confidence))
print("confidence  ordinal :",dict(conf_old))
print("            boundary:",dict(conf_new))
print(f"\nchapter-number ground truth ({acc['books']} books with >=5 numbered items on both sides):")
print(f"  ordinal : {acc['old_hits']}/{acc['old_tot']} = {100*acc['old_hits']/max(1,acc['old_tot']):.1f}% placed in the right chapter")
print(f"  boundary: {acc['new_hits']}/{acc['new_tot']} = {100*acc['new_hits']/max(1,acc['new_tot']):.1f}%")
print("\nbooks below 90% on either, and in-progress books (*):")
print(f"  {'book':<36} {'ordinal':>11} {'boundary':>11}  confidence")
seen=set()
for t,ho,to,hn,tn,co,cn in rows:
    if t in seen: continue
    seen.add(t)
    f=lambda h,x: f"{h}/{x}" if x else "  n/a"
    print(f"  {t[:36]:<36} {f(ho,to):>11} {f(hn,tn):>11}  {co} -> {cn}")
