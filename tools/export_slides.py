# -*- coding: utf-8 -*-
"""Exporta os slides do carrossel com o PROPRIO engine do site
(drawSlideToCanvas) num Chrome headless via Selenium."""
import base64, os, sys, time
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

slug = sys.argv[1]
outdir = sys.argv[2]
os.makedirs(outdir, exist_ok=True)

opts = Options()
opts.add_argument("--headless=new")
opts.add_argument("--disable-gpu")
opts.add_argument("--window-size=1280,950")
opts.binary_location = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
d = webdriver.Chrome(options=opts)
d.set_script_timeout(90)
try:
    d.get(f"https://bearlz-cms.fly.dev/c/{slug}/arquivo")
    time.sleep(3)
    # A pagina pergunta 'Quem esta editando?' via prompt() — responde pra destravar.
    # (Nao ha risco de save: o export nao edita nada.)
    for _ in range(3):
        try:
            al = d.switch_to.alert
            al.send_keys("Adre")
            al.accept()
            time.sleep(1)
        except Exception:
            break
    time.sleep(3)

    info = d.execute_async_script("""
        const done = arguments[arguments.length-1];
        (async () => {
          try {
            if (typeof loadFromServer === 'function') { try { await loadFromServer(); } catch(e){} }
            if (typeof render === 'function') { try { render(); } catch(e){} }
            if (typeof preloadImages === 'function') { try { await preloadImages(); } catch(e){} }
            done(JSON.stringify({slides: (typeof slides!=='undefined')?slides.length:-1,
                                 draw: typeof drawSlideToCanvas,
                                 W: (typeof W!=='undefined')?W:null, H: (typeof H!=='undefined')?H:null}));
          } catch(e) { done('ERR ' + e.message); }
        })();
    """)
    print("pagina:", info, flush=True)
    import json as _json
    meta = _json.loads(info)
    assert meta["slides"] > 0 and meta["draw"] == "function", "pagina nao pronta"

    for i in range(meta["slides"]):
        data_url = d.execute_async_script("""
            const i = arguments[0];
            const done = arguments[arguments.length-1];
            (async () => {
              try {
                const c = document.createElement('canvas');
                await drawSlideToCanvas(c, slides[i]);
                done(c.toDataURL('image/png'));
              } catch(e) { done('ERR ' + e.message); }
            })();
        """, i)
        if not (data_url or "").startswith("data:image/png"):
            print(f"slide {i+1}: FALHOU -> {str(data_url)[:120]}", flush=True)
            continue
        raw = base64.b64decode(data_url.split(",", 1)[1])
        p = os.path.join(outdir, f"slide-{i+1:02d}.png")
        open(p, "wb").write(raw)
        print(f"slide-{i+1:02d}.png {len(raw)//1024} KB", flush=True)
    print("EXPORT OK", flush=True)
finally:
    d.quit()
