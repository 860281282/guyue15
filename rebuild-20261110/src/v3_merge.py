"""Conservative, side-effect-free three-way merge for the experimental v3 path.

Canonicalization is comparison-only (NFC and line endings). Original input bytes
are retained in the result or conflict; ambiguous edits never auto-merge.
"""
from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata


@dataclass(frozen=True)
class MergeResult:
    content: bytes | None
    conflict: bool
    reason: str = ''


def canonical(text):
    return unicodedata.normalize('NFC', text.replace('\r\n','\n').replace('\r','\n'))


def _diff(base, side):
    b=[canonical(line) for line in base]
    s=[canonical(line) for line in side]
    out=[]
    for tag,i,j,x,y in SequenceMatcher(None,b,s,autojunk=False).get_opcodes():
        if tag=='equal':
            # Never silently rewrite the user's line endings or Unicode form.
            if base[i:j] != side[x:y]:
                return None
        else:
            out.append((i,j,side[x:y]))
    return out


def _overlap(a,b):
    a0,a1,_=a;b0,b1,_=b
    if a0==a1 and b0==b1:return a0==b0
    if a0==a1:return b0<=a0<=b1
    if b0==b1:return a0<=b0<=a1
    return max(a0,b0)<min(a1,b1)


def merge(base,local,remote):
    try:
        b,l,r=(value.decode('utf-8') for value in (base,local,remote))
    except (UnicodeError,AttributeError):
        return MergeResult(None,True,'内容不是受支持的 UTF-8')
    if local==remote:return MergeResult(local,False)
    if local==base:return MergeResult(remote,False)
    if remote==base:return MergeResult(local,False)
    if canonical(l)==canonical(r):return MergeResult(local,False)
    if canonical(l)==canonical(b) or canonical(r)==canonical(b):
        return MergeResult(None,True,'原始换行或编码形式不同，需要人工核对')
    b_lines=b.splitlines(keepends=True);l_lines=l.splitlines(keepends=True);r_lines=r.splitlines(keepends=True)
    left=_diff(b_lines,l_lines);right=_diff(b_lines,r_lines)
    if left is None or right is None:
        return MergeResult(None,True,'未修改区域的原始格式不同，需要人工核对')
    duplicate=set()
    for i,a in enumerate(left):
        for j,c in enumerate(right):
            if not _overlap(a,c):continue
            if a[0:2]==c[0:2] and canonical(''.join(a[2]))==canonical(''.join(c[2])):
                duplicate.add(j)
            else:
                return MergeResult(None,True,'两端修改了相同或相邻的内容')
    edits=left+[edit for i,edit in enumerate(right) if i not in duplicate]
    edits.sort(key=lambda edit:(edit[0],edit[1]))
    out=[];cursor=0
    for start,end,replacement in edits:
        if start<cursor:return MergeResult(None,True,'修改范围重叠')
        out.extend(b_lines[cursor:start]);out.extend(replacement);cursor=end
    out.extend(b_lines[cursor:])
    return MergeResult(''.join(out).encode('utf-8'),False)
