"""Strip em-dash (—) from slide text in carousel HTMLs.
Only touches content inside `text:` backticks; leaves titles, comments, code alone.
"""
import glob, re, sys

files = glob.glob(r'carrosseis/carrossel-*.html')
total = 0
for p in files:
    s = open(p, encoding='utf-8').read()
    orig = s
    # Find each text:`...` block and replace em-dash inside it
    def fix_text(m):
        body = m.group(1)
        # Replace " — " with ", " (most common case)
        body = body.replace(' — ', ', ')
        body = body.replace(' —', ',')
        body = body.replace('— ', '')
        body = body.replace('—', '')
        return f'text:`{body}`'
    s = re.sub(r'text:`((?:[^`\\]|\\.)*)`', fix_text, s, flags=re.DOTALL)
    if s != orig:
        open(p, 'w', encoding='utf-8').write(s)
        removed = orig.count('—') - s.count('—')
        total += removed
        sys.stderr.write(f'{p}: removed {removed}\n')
sys.stderr.write(f'Total removed: {total}\n')
