function parseBold(t){
  const p=[];const r=/\*\*(.*?)\*\*/g;let l=0,m;
  while((m=r.exec(t))!==null){if(m.index>l)p.push({t:t.slice(l,m.index),b:false});p.push({t:m[1],b:true});l=r.lastIndex}
  if(l<t.length)p.push({t:t.slice(l),b:false});
  return p;
}
function toHTML(text){
  return parseBold(text).map(p=>{
    const esc=p.t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    return p.b?`<strong>${esc}</strong>`:esc;
  }).join('');
}
// Regex de linha de bullet: comeca com • ou - (ou *) seguido de espaco.
const BULLET_RE=/^\s*[•\-\*]\s+(.*)$/;
// Converte o texto do slide em HTML pro display/preview.
// - Paragrafos separados por \n\n viram blocos com margem.
// - Dentro de um paragrafo, linhas que comecam com • ou - viram bullets
//   com recuo pendurado (hanging indent) via classe .slide-bullet.
function textToDisplayHTML(text){
  return (text||'').split(/\n\n+/).map(para=>{
    const linhas=para.split('\n');
    const temBullet=linhas.some(l=>BULLET_RE.test(l));
    if(temBullet){
      const inner=linhas.map(l=>{
        const bm=BULLET_RE.exec(l);
        if(bm)return `<span class="slide-bullet">${toHTML(bm[1])}</span>`;
        if(!l.trim())return '';
        return `<span style="display:block">${toHTML(l)}</span>`;
      }).join('');
      return `<span style="display:block;margin-bottom:16px">${inner}</span>`;
    }
    return `<span style="display:block;margin-bottom:16px">${toHTML(para)}</span>`;
  }).join('');
}

/* ── Auto-save / Auto-load (local + servidor compartilhado) ── */
const LS_KEY=window.CAROUSEL_LS_KEY||'bearlz_carousel_v1';
let _saveTimer,_serverSaveTimer,_lastServerUpdate=null,_myAutor='Anonimo';

/* ── Status de revisao por slide ────────────────────────────────
   Guardado em localStorage separado do estado do carrossel pra nao
   poluir o sync com o servidor. Chave indexada por HASH do conteudo
   do slide — se o texto mudar, o hash muda e o status some
   automaticamente (forca nova revisao). */
const REVIEW_LS_KEY=(window.CAROUSEL_LS_KEY||'bearlz_carousel_v1')+'_review';
function _slideHash(text){
  if(!text)return '';
  let h=0;
  const s=text.normalize?text.normalize('NFKC'):text;
  for(let i=0;i<s.length;i++){h=((h<<5)-h+s.charCodeAt(i))|0;}
  return h.toString(36)+'_'+s.length;
}
function _getReviewMap(){
  try{const r=localStorage.getItem(REVIEW_LS_KEY);return r?JSON.parse(r):{};}
  catch(e){return {};}
}
function _saveReviewMap(m){
  try{localStorage.setItem(REVIEW_LS_KEY,JSON.stringify(m));}catch(e){}
}
function isSlideReviewed(idx){
  const s=slides[idx];if(!s)return false;
  const h=_slideHash(s.text||'');if(!h)return false;
  const m=_getReviewMap();
  return !!(m[h]&&m[h].reviewed);
}
function toggleSlideReviewed(){
  const s=slides[cur];if(!s)return;
  const h=_slideHash(s.text||'');if(!h)return;
  const m=_getReviewMap();
  if(m[h]&&m[h].reviewed)delete m[h];
  else m[h]={reviewed:true,reviewedAt:Date.now()};
  _saveReviewMap(m);
  render();
}
window.toggleSlideReviewed=toggleSlideReviewed;
window.isSlideReviewed=isSlideReviewed;

function _getUserIdentity(){
  let u=localStorage.getItem('bearlz_user');
  if(!u){
    u=prompt('Quem está editando? (digite: Adre ou Gabriel)')||'Anonimo';
    u=u.trim()||'Anonimo';
    localStorage.setItem('bearlz_user',u);
  }
  _myAutor=u;
  const ui=document.getElementById('userIndicator');
  if(ui)ui.textContent='Você: '+_myAutor;
}

function _buildState(){
  return{
    slides:slides.map(s=>{
      const c={...s};
      delete c._autoFit; // efemero (decisao de render), nao persiste no estado
      if(c.image&&c.image.startsWith('data:image/svg+xml'))c.image=null;
      return c;
    }),
    profile:{...profile},
    avatar:avatarDataUrl,
    style:(typeof getTextStyle==='function'?getTextStyle():null),
    v:2
  };
}

function autoSave(){
  clearTimeout(_saveTimer);
  _saveTimer=setTimeout(()=>{
    try{
      localStorage.setItem(LS_KEY,JSON.stringify(_buildState()));
      flashSaved();
    }catch(e){
      try{
        const lite=_buildState();
        lite.slides=lite.slides.map(s=>{const c={...s};if(c.image&&c.image.startsWith('data:'))c.image=null;return c;});
        localStorage.setItem(LS_KEY,JSON.stringify(lite));
        flashSaved();
      }catch(e2){}
    }
    saveToServer();
  },400);
}

function saveToServer(){
  if(!window.CAROUSEL_SLUG)return;
  clearTimeout(_serverSaveTimer);
  _serverSaveTimer=setTimeout(async()=>{
    try{
      // Concorrencia otimista: antes de gravar, confere se o servidor ja tem
      // uma versao mais nova de OUTRA aba/pessoa. Se tiver, NAO sobrescreve —
      // recarrega (ou cancela), evitando o clobber de "ultima gravacao vence".
      try{
        const chk=await fetch('/api/carrossel/'+window.CAROUSEL_SLUG+'/state');
        if(chk.ok){
          const cd=await chk.json();
          if(cd&&cd.updated_at&&_lastServerUpdate&&cd.updated_at!==_lastServerUpdate&&cd.autor!==_myAutor){
            _showCloud('⚠ Versão mais nova de '+(cd.autor||'outro'));
            if(confirm((cd.autor||'Outra pessoa')+' salvou uma versão mais nova deste carrossel. Recarregar para não sobrescrever?')){location.reload();}
            return;
          }
        }
      }catch(e){}
      _showCloud('Salvando...');
      const r=await fetch('/api/carrossel/'+window.CAROUSEL_SLUG+'/save',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({state:_buildState(),autor:_myAutor})
      });
      if(r.ok){
        const d=await r.json();
        _lastServerUpdate=d.updated_at;
        _showCloud('✓ Salvo por '+_myAutor);
      }else{_showCloud('⚠ Erro salvar');}
    }catch(e){_showCloud('⚠ Offline');}
  },900);
}

async function loadFromServer(){
  if(!window.CAROUSEL_SLUG)return false;
  try{
    const r=await fetch('/api/carrossel/'+window.CAROUSEL_SLUG+'/state');
    if(!r.ok)return false;
    const d=await r.json();
    if(!d||!d.state)return false;
    _lastServerUpdate=d.updated_at;
    const st=d.state;
    if(st.slides){
      st.slides.forEach((saved,i)=>{
        // Se o estado salvo tem MAIS slides que o template, cria os extras.
        // Antes esses slides eram silenciosamente descartados — usuario
        // que adicionasse slides 12+ via "+ Slide" perdia tudo no reload.
        if(i>=slides.length){
          const novoId=(slides.length>0?Math.max(...slides.map(x=>x.id)):0)+1;
          const novo={
            id:saved.id||novoId,
            text:saved.text||'',
            image:(saved.image && !String(saved.image).startsWith('data:image/svg'))?saved.image:null,
            zoom:saved.zoom||1,
            ox:saved.ox!=null?saved.ox:50,
            oy:saved.oy!=null?saved.oy:50,
            imgH:saved.imgH??null,
            fit:saved.fit||null,
            video:saved.video||null,
            imgNW:null,imgNH:null
          };
          slides.push(novo);
          return;
        }
        if(saved.text!=null)slides[i].text=saved.text;
        if(saved.zoom!=null)slides[i].zoom=saved.zoom;
        if(saved.ox!=null)slides[i].ox=saved.ox;
        if(saved.oy!=null)slides[i].oy=saved.oy;
        slides[i].imgH=saved.imgH??null;
        // Modo livre (Canva-style): freeX/Y em px absolutos
        if('freeX' in saved)slides[i].freeX=saved.freeX;
        if('freeY' in saved)slides[i].freeY=saved.freeY;
        // imgMarginTop: deixa imagem invadir espaco acima (margin negativa)
        if('imgMarginTop' in saved)slides[i].imgMarginTop=saved.imgMarginTop;
        // gapTextImg: usuario ajusta gap entre texto e imagem manualmente
        if('gapTextImg' in saved)slides[i].gapTextImg=saved.gapTextImg;
        if('fit' in saved)slides[i].fit=saved.fit;
        if('video' in saved)slides[i].video=saved.video;
        if('image' in saved){
          if(saved.image===null){
            slides[i].image=null;
            slides[i].imgNW=null;
            slides[i].imgNH=null;
          }else if(saved.image && !saved.image.startsWith('data:image/svg')){
            slides[i].image=saved.image;
          }
        }
      });
      // Se o estado salvo tem MENOS slides que o template (usuario removeu
      // alguns), trunca o array local pra refletir.
      if(st.slides.length < slides.length){
        slides.length=st.slides.length;
      }
    }
    if(st.profile){profile.name=st.profile.name;profile.handle=st.profile.handle;}
    if(st.avatar)avatarDataUrl=st.avatar;
    if(st.style){
      const ff=document.getElementById('fontFamily'),fs=document.getElementById('fontSize'),lh=document.getElementById('lineHeight'),pg=document.getElementById('paraGap');
      if(ff&&st.style.family)ff.value=st.style.family;
      if(fs&&st.style.size)fs.value=st.style.size;
      if(lh&&st.style.lh)lh.value=st.style.lh;
      if(pg&&st.style.pg)pg.value=st.style.pg;
    }
    _showCloud('Carregado: '+d.autor+' '+_timeAgo(d.updated_at));
    return true;
  }catch(e){return false;}
}

async function checkRemoteChanges(){
  if(!window.CAROUSEL_SLUG)return;
  try{
    const r=await fetch('/api/carrossel/'+window.CAROUSEL_SLUG+'/state');
    if(!r.ok)return;
    const d=await r.json();
    if(!d.updated_at||d.updated_at===_lastServerUpdate)return;
    if(d.autor===_myAutor){_lastServerUpdate=d.updated_at;return;}
    if(confirm(d.autor+' acabou de editar esse carrossel.\n\nRecarregar para ver as mudanças? (Suas edições locais não salvas podem ser perdidas)')){
      location.reload();
    }else{_lastServerUpdate=d.updated_at;}
  }catch(e){}
}

function _showCloud(msg){
  const el=document.getElementById('cloudStatus');
  if(el)el.textContent=msg;
}

function _timeAgo(iso){
  try{
    const s=Math.round((Date.now()-new Date(iso).getTime())/1000);
    if(s<60)return 'há '+s+'s';
    if(s<3600)return 'há '+Math.round(s/60)+'min';
    return 'há '+Math.round(s/3600)+'h';
  }catch(e){return ''}
}

async function initHydrate(){
  _getUserIdentity();
  const serverOK=await loadFromServer();
  if(!serverOK)autoLoad();
  if(typeof render==='function')render();
  if(typeof loadTextStyle==='function')loadTextStyle();
  if(typeof checkOverflow==='function')checkOverflow();
  setInterval(checkRemoteChanges,15000);
}
function flashSaved(){
  const el=document.getElementById('saveIndicator');
  if(!el)return;
  el.textContent='✓ Salvo';
  clearTimeout(el._t);
  el._t=setTimeout(()=>{el.textContent='';},2200);
}
function autoLoad(){
  try{
    const raw=localStorage.getItem(LS_KEY);
    if(!raw)return;
    const state=JSON.parse(raw);
    // Aceita v:1 (legado) e v:2 (atual). Nao quebra dados antigos.
    if(state.v!==1 && state.v!==2)return;
    if(state.slides){
      state.slides.forEach((saved,i)=>{
        if(i>=slides.length){
          const novoId=(slides.length>0?Math.max(...slides.map(x=>x.id)):0)+1;
          slides.push({
            id:saved.id||novoId,
            text:saved.text||'',
            image:(saved.image && !String(saved.image).startsWith('data:image/svg'))?saved.image:null,
            zoom:saved.zoom||1,
            ox:saved.ox!=null?saved.ox:50,
            oy:saved.oy!=null?saved.oy:50,
            imgH:saved.imgH??null,
            fit:saved.fit||null,
            video:saved.video||null,
            imgNW:null,imgNH:null,
            freeX:saved.freeX??null,freeY:saved.freeY??null
          });
          return;
        }
        if(saved.text!=null)slides[i].text=saved.text;
        if(saved.zoom!=null)slides[i].zoom=saved.zoom;
        if(saved.ox!=null)slides[i].ox=saved.ox;
        if(saved.oy!=null)slides[i].oy=saved.oy;
        slides[i].imgH=saved.imgH??null;
        // Modo livre (Canva-style): freeX/Y em px absolutos
        if('freeX' in saved)slides[i].freeX=saved.freeX;
        if('freeY' in saved)slides[i].freeY=saved.freeY;
        // imgMarginTop: deixa imagem invadir espaco acima (margin negativa)
        if('imgMarginTop' in saved)slides[i].imgMarginTop=saved.imgMarginTop;
        // gapTextImg: usuario ajusta gap entre texto e imagem manualmente
        if('gapTextImg' in saved)slides[i].gapTextImg=saved.gapTextImg;
        if('fit' in saved)slides[i].fit=saved.fit;
        if('video' in saved)slides[i].video=saved.video;
        if('image' in saved){
          if(saved.image===null){
            slides[i].image=null;
            slides[i].imgNW=null;
            slides[i].imgNH=null;
          }else if(saved.image && !saved.image.startsWith('data:image/svg')){
            slides[i].image=saved.image;
          }
        }
      });
      if(state.slides.length < slides.length){
        slides.length=state.slides.length;
      }
    }
    if(state.profile){profile.name=state.profile.name;profile.handle=state.profile.handle;}
    if(state.avatar)avatarDataUrl=state.avatar;
  }catch(e){}
}
function clearSaved(){if(confirm('Apagar dados salvos e voltar ao original?')){localStorage.removeItem(LS_KEY);location.reload();}}

/* ── Quick image controls ── */
function setPos(ox,oy){
  slides[cur].ox=ox;slides[cur].oy=oy;
  applyBgSize(slides[cur]); // reposiciona o <img> real (era backgroundPosition)
  updateImgCtrlUI(slides[cur]);
  autoSave();
}
function setZoomPreset(z){
  slides[cur].zoom=z;
  applyBgSize(slides[cur]);
  updateImgCtrlUI(slides[cur]);
  autoSave();
}
function setHPreset(h){
  slides[cur].imgH=h;
  const cont=document.getElementById('imgContainer');
  if(cont){if(h){cont.style.height=h+'px';cont.style.flex='none';}else{cont.style.height='';cont.style.flex='1';}}
  updateImgCtrlUI(slides[cur]);
  autoSave();
}
function setFit(mode){
  slides[cur].fit=mode;
  const cont=document.getElementById('imgContainer');
  if(cont){
    // Apos refactor pra <img>, fit eh aplicado dentro do applyBgSize.
    // Aqui so muda o background do container (cinza claro pra contain mostrar
    // letras laterais; transparente pra cover).
    cont.style.background = mode==='contain' ? '#f8f8f8' : 'transparent';
    applyBgSize(slides[cur]);
  }
  updateImgCtrlUI(slides[cur]);
  autoSave();
}
function toggleFineCtrl(){
  const w=document.getElementById('fineCtrlWrap');
  const btn=document.querySelector('.fine-toggle');
  if(!w)return;
  const open=w.style.display!=='none';
  w.style.display=open?'none':'block';
  if(btn)btn.textContent=(open?'▸':'▾')+' Ajuste fino (sliders)';
}

/* ── Render ── */
function render(){
  const s=slides[cur];
  const total=slides.length;
  document.getElementById('subtitle').textContent=`${total} slides · Gabriel Bearlz`;
  const td=document.getElementById('topDots');
  // Chips numerados, draggable pra reordenar. Numero do slide visivel
  // pra usuario saber qual eh qual. Mais facil de agarrar que dot 7px.
  // Cor do chip indica status de revisao:
  //   .reviewed → verde (slide ja foi marcado como revisado)
  //   .pending  → vermelho (ainda nao revisado ou texto mudou desde)
  td.innerHTML=slides.map((_,i)=>{
    const rev=isSlideReviewed(i);
    const revCls=rev?' reviewed':' pending';
    const revLbl=rev?' ✓ revisado':' • pendente';
    return `<button class="top-dot${i===cur?' active':''}${revCls}" data-idx="${i}" draggable="true" onclick="goTo(${i})" title="Slide ${i+1}${revLbl} — clique pra navegar ou arraste pra reordenar">${i+1}</button>`;
  }).join('');
  _bindDotsDragAndDrop();
  document.getElementById('btnPrev').disabled=cur===0;
  document.getElementById('btnNext').disabled=cur===total-1;
  document.getElementById('btnRem').disabled=total<=1;
  document.getElementById('dispName').textContent=profile.name;
  document.getElementById('dispHandle').textContent=profile.handle;
  if(avatarDataUrl){
    document.getElementById('avatarDisp').innerHTML=`<img src="${avatarDataUrl}"/>`;
  }else{
    document.getElementById('avatarDisp').innerHTML=`<span id="avatarLetter">G</span>`;
  }
  // Modo edit: text-area ganha 'editing-mode' (z-index + bg) e card ganha
  // 'editing-text' (esconde imagem+footer pra liberar espaco visivel).
  // Resultado: textarea + botao Salvar sempre 100% visiveis.
  const textAreaEl=document.getElementById('textArea');
  const cardEl=document.getElementById('theCard')||document.querySelector('.card');
  if(editingText){
    document.getElementById('textDisplay').style.display='none';
    document.getElementById('textEditArea').style.display='block';
    document.getElementById('editTA').value=s.text;
    if(textAreaEl)textAreaEl.classList.add('editing-mode');
    if(cardEl)cardEl.classList.add('editing-text');
    // Injeta Revisar PT + Polir + char-count em posts antigos
    // Roda DEPOIS do display:block pra garantir que o .edit-actions
    // esteja visivel quando a injecao acontece.
    setTimeout(_ensureEditButtons, 20);
    // Auto-scroll pro textarea ficar visivel no iframe
    setTimeout(()=>{
      const ta=document.getElementById('editTA');
      if(ta){
        ta.scrollIntoView({behavior:'smooth',block:'center'});
      }
    },80);
  }else{
    document.getElementById('textDisplay').style.display='block';
    document.getElementById('textEditArea').style.display='none';
    document.getElementById('textDisplay').innerHTML=textToDisplayHTML(s.text);
    if(textAreaEl)textAreaEl.classList.remove('editing-mode');
    if(cardEl)cardEl.classList.remove('editing-text');
  }
  renderImgSection();
  const cd=document.getElementById('cardDots');
  cd.innerHTML=slides.map((_,i)=>`<div class="card-dot${i===cur?' active':''}"></div>`).join('');
  document.getElementById('footerCounter').textContent=`${cur+1}/${total}`;
  document.getElementById('rangeZoom').value=s.zoom||1;
  document.getElementById('valZoom').textContent=Math.round((s.zoom||1)*100)+'%';
  document.getElementById('rangeOx').value=s.ox||50;
  document.getElementById('valOx').textContent=(s.ox||50)+'%';
  document.getElementById('rangeOy').value=s.oy||50;
  document.getElementById('valOy').textContent=(s.oy||50)+'%';
  document.getElementById('rangeH').value=s.imgH||240;
  document.getElementById('valH').textContent=s.imgH?(s.imgH+'px'):'Auto';
}

function getEffectiveFit(s){
  // SVG charts always use contain to show the full chart
  if(s.image&&s.image.startsWith('data:image/svg'))return'contain';
  // Video (grafico animado): mostra inteiro por padrao
  if(s.video&&!s.fit)return'contain';
  // Auto-fit: imagem LARGA (grafico/panorama, ar>=1.7) detectada no load
  // vira contain sozinha — acaba o "grafico cortado ate ajustar na mao".
  if(!s.fit&&s._autoFit)return s._autoFit;
  return s.fit||'cover';
}

function renderImgSection(){
  const s=slides[cur];
  const sec=document.getElementById('imgSection');
  const panel=document.getElementById('imgCtrlPanel');
  if(s.video){
    // Slide com VIDEO (grafico animado): toca em loop, mudo, autoplay —
    // como no Instagram. O campo image segue como poster pro export PNG;
    // o MP4 entra no ZIP pelo downloadZip.
    const fit=getEffectiveFit(s);
    const hStyle=s.imgH?`height:${s.imgH}px;flex:none`:'';
    sec.innerHTML=`<div class="img-section" style="position:relative">
      <div class="img-container" id="imgContainer" style="${hStyle}">
        <video id="vidReal" class="img-real" src="${s.video}" autoplay muted loop playsinline
          style="width:100%;height:100%;object-fit:contain;background:#0b0e14;display:block"></video>
        <div style="position:absolute;top:8px;right:8px;background:rgba(0,0,0,.55);color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:5px;letter-spacing:.5px;pointer-events:none">🎬 VÍDEO</div>
      </div>
    </div>`;
    // Centralizacao automatica: o container ganha a ALTURA da proporcao do
    // video (nada de corte nem barras), a nao ser que o usuario tenha
    // ajustado a altura na mao (s.imgH).
    const vEl=document.getElementById('vidReal');
    if(vEl&&!s.imgH){
      vEl.addEventListener('loadedmetadata',()=>{
        const cont=vEl.parentElement;
        if(!cont||!vEl.videoWidth)return;
        const h=Math.max(180,Math.min(560,Math.round(cont.clientWidth*vEl.videoHeight/vEl.videoWidth)));
        cont.style.height=h+'px';cont.style.flex='none';
      });
    }
    _ensureCardDeleteBtn();
    _showCardDeleteBtn(true);
    document.getElementById('btnAjustar').style.display='none';
    panel.style.display='none';
  }else if(s.image){
    const fit=getEffectiveFit(s);
    const hStyle=s.imgH?`height:${s.imgH}px;flex:none`:'';
    const bgColor=fit==='contain'?'#f8f8f8':'transparent';
    const freeClass=fit==='free'?' free-edit':'';
    // Adiciona crossorigin pra URLs externas (Pexels, etc) pra que html2canvas
    // nao "taint" o canvas e o export inclua a imagem. Imagens data: nao
    // precisam (nao tem CORS).
    const isExternal = s.image && !s.image.startsWith('data:');
    const crossAttr = isExternal ? ' crossorigin="anonymous"' : '';
    sec.innerHTML=`<div class="img-section" style="position:relative">
      <div class="img-container${freeClass}" id="imgContainer" style="${hStyle};background:${bgColor}">
        <img id="imgReal" class="img-real"${crossAttr} src="${s.image}" alt="" draggable="false" style="visibility:hidden">
        <div class="img-resize-handle img-resize-handle-top" id="imgResizeHandleTop" title="Arraste pra cima pra esticar / pra baixo pra encolher"></div>
        <div class="img-resize-handle img-resize-handle-bottom" id="imgResizeHandle" title="Arraste pra baixo pra esticar / pra cima pra encolher"></div>
      </div>
    </div>`;
    // Botao X vive ANCORADO NO .card (nao dentro de img-section/img-container).
    // Imune a overflow:hidden e a qualquer posicionamento que a imagem faca.
    _ensureCardDeleteBtn();
    _showCardDeleteBtn(true);
    requestAnimationFrame(()=>{
      _applyImgFraming(s); // aplica margin-top + altura customizadas
      applyBgSize(s);
      const im=document.getElementById('imgReal');
      if(im){
        im.style.visibility='visible';
        // Auto-fit: grafico/panorama (largura >= 1.7x altura) mostra INTEIRO
        // sem o usuario precisar clicar em 'contain'. So quando nao ha fit
        // manual salvo; re-renderiza uma vez ao detectar.
        const decide=()=>{
          if(s.fit||s._autoFit)return;
          const isChartUrl=/\/static\/charts\/|fredgraph|stooq\.com|quickchart/.test(s.image||'');
          const wide=im.naturalWidth&&im.naturalHeight&&im.naturalWidth/im.naturalHeight>=1.9;
          if(isChartUrl||wide){s._autoFit='contain';render();}
        };
        if(im.complete)decide();else im.addEventListener('load',decide,{once:true});
      }
    });
    document.getElementById('btnAjustar').style.display='inline';
    // Drag funciona em cover E free; contain a posicao eh fixa
    if(fit!=='contain')initDrag();
    initWheelZoom();
    initPinchZoom();
    initKeyboardImg();
    initResizeHandle();
    // Sincroniza estado visual do botao "Editar livre"
    setTimeout(()=>{
      const btn=document.getElementById('btnFreeEdit');
      if(btn){
        btn.classList.toggle('active',fit==='free');
        btn.textContent=fit==='free'?'Sair do livre':'Editar livre';
      }
    },10);
    updateImgCtrlUI(s);
  }else{
    sec.innerHTML=`<div class="img-section">
      <div class="img-placeholder" onclick="document.getElementById('fileImg').click()">
        <div class="img-placeholder-icon">🖼</div>
        <div class="img-placeholder-txt">Adicionar imagem</div>
      </div>
    </div>`;
    document.getElementById('btnAjustar').style.display='none';
    panel.style.display='none';
    // Sem imagem -> esconde o botao X global do card
    _showCardDeleteBtn(false);
  }
}

function updateImgCtrlUI(s){
  const fit=getEffectiveFit(s);
  const isSVG=s.image&&s.image.startsWith('data:image/svg');
  const bc=document.getElementById('btnFitCover');
  const bn=document.getElementById('btnFitContain');
  if(bc)bc.className='preset-btn'+(fit==='cover'?' active':'');
  if(bn)bn.className='preset-btn'+(fit==='contain'?' active':'');
  // Highlight active position button
  const grid=document.getElementById('posGrid');
  if(grid){
    const ox=s.ox||50,oy=s.oy||50;
    const positions=[[15,15],[50,15],[85,15],[15,50],[50,50],[85,50],[15,85],[50,85],[85,85]];
    Array.from(grid.querySelectorAll('button')).forEach((btn,i)=>{
      const [px,py]=positions[i];
      btn.className=((Math.abs(px-ox)<20)&&(Math.abs(py-oy)<20))?'pos-active':'';
    });
  }
  // Highlight zoom
  const zb=document.getElementById('zoomBtns');
  if(zb){
    const z=s.zoom||1;
    const zvals=[1,1.3,1.7,2];
    Array.from(zb.querySelectorAll('button')).forEach((btn,i)=>{
      btn.className='preset-btn'+(Math.abs(zvals[i]-z)<0.15?' active':'');
    });
  }
  // Highlight height
  const hb=document.getElementById('hBtns');
  if(hb){
    const h=s.imgH;
    const hvals=[120,200,300,420,null];
    Array.from(hb.querySelectorAll('button')).forEach((btn,i)=>{
      btn.className='preset-btn'+(h===hvals[i]?' active':'');
    });
  }
  // Sync fine sliders
  const rz=document.getElementById('rangeZoom');if(rz)rz.value=s.zoom||1;
  const vz=document.getElementById('valZoom');if(vz)vz.textContent=Math.round((s.zoom||1)*100)+'%';
  const rx=document.getElementById('rangeOx');if(rx)rx.value=s.ox||50;
  const vx=document.getElementById('valOx');if(vx)vx.textContent=(s.ox||50)+'%';
  const ry=document.getElementById('rangeOy');if(ry)ry.value=s.oy||50;
  const vy=document.getElementById('valOy');if(vy)vy.textContent=(s.oy||50)+'%';
  const rh=document.getElementById('rangeH');if(rh)rh.value=s.imgH||240;
  const vh=document.getElementById('valH');if(vh)vh.textContent=s.imgH?(s.imgH+'px'):'Auto';
}

/* ── Drag to pan ──
   Em modo cover/contain: atualiza ox/oy 0-100 com clamp (movimento
     restrito ao crop).
   Em modo free: atualiza freeX/freeY em pixels absolutos, sem clamp
     — imagem pode sair do container. */
function initDrag(){
  const cont=document.getElementById('imgContainer');
  if(!cont)return;
  let dragging=false,startX=0,startY=0,startOx=0,startOy=0,startFx=0,startFy=0,modeFree=false;
  const onDown=(e)=>{
    if(e.target&&e.target.classList&&e.target.classList.contains('img-overlay-btn'))return;
    if(e.touches&&e.touches.length>=2)return;
    e.preventDefault();dragging=true;
    const pt=e.touches?e.touches[0]:e;
    const s=slides[cur];
    startX=pt.clientX;startY=pt.clientY;
    modeFree=getEffectiveFit(s)==='free';
    if(modeFree){
      startFx=s.freeX||0;startFy=s.freeY||0;
    }else{
      startOx=s.ox||50;startOy=s.oy||50;
    }
    cont.style.cursor='grabbing';
    window.addEventListener('mousemove',onMove);window.addEventListener('mouseup',onUp);
    window.addEventListener('touchmove',onMove,{passive:false});window.addEventListener('touchend',onUp);
  };
  let rafPending=false;
  const onMove=(e)=>{
    if(!dragging)return;
    if(e.touches&&e.touches.length>=2){onUp();return;}
    e.preventDefault();
    const pt=e.touches?e.touches[0]:e;
    const cx=pt.clientX, cy=pt.clientY;
    if(rafPending)return; // throttle: 1 update por frame
    rafPending=true;
    requestAnimationFrame(()=>{
      rafPending=false;
      const s=slides[cur];
      if(modeFree){
        s.freeX=Math.round(startFx+(cx-startX));
        s.freeY=Math.round(startFy+(cy-startY));
      }else{
        const r=cont.getBoundingClientRect();
        const dx=((cx-startX)/r.width)*100;
        const dy=((cy-startY)/r.height)*100;
        s.ox=Math.max(0,Math.min(100,Math.round(startOx-dx)));
        s.oy=Math.max(0,Math.min(100,Math.round(startOy-dy)));
      }
      applyBgSize(s);
    });
  };
  const onUp=()=>{
    if(!dragging)return;
    dragging=false;
    cont.style.cursor='grab';
    window.removeEventListener('mousemove',onMove);window.removeEventListener('mouseup',onUp);
    window.removeEventListener('touchmove',onMove);window.removeEventListener('touchend',onUp);
    updateImgCtrlUI(slides[cur]);autoSave();
  };
  cont.addEventListener('mousedown',onDown);
  cont.addEventListener('touchstart',onDown,{passive:false});
}

/* ── Resize handles (Canva-style) ──
   Handle BOTTOM: estica/encolhe altura via imgH.
   Handle TOP: faz a imagem SUBIR invadindo o espaco do texto (via
   margin-top negativa). Permite ocupar espacos do card que antes
   ficavam fixos pelo flex layout. */

/* Maximizar imagem: puxa pra CIMA invadindo o espaco do texto (suave),
   E estica pra BAIXO ate o LIMITE DO POST (linha vermelha em 525px).
   Resultado: imagem ocupa o maximo possivel do card. */
function maximizarImg(){
  const s=slides[cur];
  if(!s)return;
  const card=document.getElementById('theCard')||document.querySelector('.card');
  const sec=document.querySelector('.img-section');
  if(!card||!sec)return;
  // 1. Pra cima: invade o espaco do texto com margin negativa de 20px
  // (suave, mantem profile e texto sempre visiveis acima da imagem)
  s.imgMarginTop=-20;
  // 2. Pra baixo: calcula altura ate a linha vermelha (525px do card)
  const cardRect=card.getBoundingClientRect();
  const secRect =sec.getBoundingClientRect();
  const topDoSec=secRect.top-cardRect.top;
  const limite=525;
  // Altura disponivel = limite - top original + os 20px que ganhou pra cima
  const alturaDisp=Math.max(150,limite-topDoSec+20);
  s.imgH=Math.round(alturaDisp);
  _applyImgFraming(s);
  applyBgSize(s);
  updateImgCtrlUI(s);
  autoSave();
  if(typeof checkOverflow==='function')checkOverflow();
  if(typeof setStatus==='function')setStatus(`Imagem expandida (${s.imgH}px, sobe 60px)`);
  setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},2800);
}
window.maximizarImg=maximizarImg;

function _applyImgFraming(s){
  const sec=document.querySelector('.img-section');
  const cont=document.getElementById('imgContainer');
  const gap=document.getElementById('gapHandle');
  if(!cont)return;
  // RESET DEFENSIVO: zera todos os estilos inline antes de aplicar.
  // Evita estado residual entre slides (slide A com imgMarginTop=-50 troca
  // pro slide B sem custom — antes herdava o -50).
  if(sec){sec.style.marginTop='';}
  if(gap){gap.style.height='';gap.style.marginTop='';}
  cont.style.paddingTop='';
  cont.style.height='';
  cont.style.flex='';

  // Aplica os custom de cada slide (se houver)
  const mt=s.imgMarginTop||0;
  if(sec&&mt!==0)sec.style.marginTop=mt+'px';

  const g=s.gapTextImg||0;
  if(gap){
    gap.style.height=(g<0?4:Math.max(4,8+g))+'px';
    if(g<0)gap.style.marginTop=g+'px';
  }
  if(cont&&g>0)cont.style.paddingTop=g+'px';

  if(s.imgH){
    cont.style.height=s.imgH+'px';
    cont.style.flex='none';
  }
}

/* Injeta um botao X de "apagar imagem" diretamente no .card (nao dentro
   do imgSection). Fica ancorado ao topo-right do card, position:absolute,
   z-index astronomico. Imune a qualquer overflow:hidden ou margem
   negativa que afete o imgSection. */
function _ensureCardDeleteBtn(){
  if(document.getElementById('cardDeleteImgBtn'))return;
  const card=document.getElementById('theCard')||document.querySelector('.card');
  if(!card)return;
  // Garante que o card eh containing block pro absolute do botao
  const cs=getComputedStyle(card);
  if(cs.position==='static')card.style.position='relative';
  const btn=document.createElement('button');
  btn.id='cardDeleteImgBtn';
  btn.type='button';
  btn.innerHTML='×';
  btn.title='Apagar imagem deste slide';
  btn.setAttribute('aria-label','Apagar imagem');
  btn.onclick=(e)=>{e.preventDefault();e.stopPropagation();if(typeof clearImage==='function')clearImage();};
  // Estilos inline com !important via cssText pra ninguem sobrescrever
  btn.style.cssText=
    'position:absolute!important;'+
    'top:12px!important;'+
    'right:12px!important;'+
    'width:38px!important;'+
    'height:38px!important;'+
    'border-radius:50%!important;'+
    'background:#dc2626!important;'+
    'color:#fff!important;'+
    'border:3px solid #fff!important;'+
    'font-size:22px!important;'+
    'font-weight:900!important;'+
    'cursor:pointer!important;'+
    'display:flex!important;'+
    'align-items:center!important;'+
    'justify-content:center!important;'+
    'z-index:99999!important;'+
    'box-shadow:0 4px 14px rgba(0,0,0,.5)!important;'+
    'line-height:1!important;'+
    'padding:0!important;'+
    'visibility:visible!important;'+
    'opacity:1!important;'+
    'pointer-events:auto!important;'+
    'font-family:inherit!important;';
  card.appendChild(btn);
}
function _showCardDeleteBtn(show){
  const btn=document.getElementById('cardDeleteImgBtn');
  if(!btn)return;
  btn.style.setProperty('display',show?'flex':'none','important');
}

/* Injeta o .gap-handle no DOM se nao existir (posts antigos foram
   gerados antes do template ter esse handle). Roda no boot. */
function _ensureGapHandle(){
  if(document.getElementById('gapHandle'))return true;
  const textArea=document.getElementById('textArea')||document.querySelector('.text-area');
  const imgSection=document.getElementById('imgSection');
  if(!textArea||!imgSection||!textArea.parentNode)return false;
  const handle=document.createElement('div');
  handle.id='gapHandle';
  handle.className='gap-handle';
  handle.title='Arraste pra ajustar o espaço entre texto e imagem';
  // Insere ANTES do imgSection (entre text-area e img-section)
  textArea.parentNode.insertBefore(handle,imgSection);
  return true;
}

/* Drag no handle entre texto e imagem — ajusta s.gapTextImg em px.
   Drag pra cima diminui gap (pode ficar negativo: imagem encosta no
   texto OU invade); drag pra baixo aumenta gap. */
function initGapHandle(){
  if(!_ensureGapHandle())return;
  const handle=document.getElementById('gapHandle');
  if(!handle||handle._bound)return;
  handle._bound=true;
  let dragging=false,startY=0,startGap=0,rafPending=false;
  const onDown=(e)=>{
    e.preventDefault();e.stopPropagation();
    dragging=true;
    const pt=e.touches?e.touches[0]:e;
    startY=pt.clientY;
    startGap=slides[cur].gapTextImg||0;
    document.body.style.cursor='ns-resize';
    window.addEventListener('mousemove',onMove);window.addEventListener('mouseup',onUp);
    window.addEventListener('touchmove',onMove,{passive:false});window.addEventListener('touchend',onUp);
  };
  const onMove=(e)=>{
    if(!dragging)return;
    e.preventDefault();
    const pt=e.touches?e.touches[0]:e;
    const cy=pt.clientY;
    if(rafPending)return;
    rafPending=true;
    requestAnimationFrame(()=>{
      rafPending=false;
      const delta=cy-startY;
      slides[cur].gapTextImg=Math.round(Math.max(-150,Math.min(200,startGap+delta)));
      _applyImgFraming(slides[cur]);
      applyBgSize(slides[cur]);
    });
  };
  const onUp=()=>{
    if(!dragging)return;
    dragging=false;
    document.body.style.cursor='';
    window.removeEventListener('mousemove',onMove);window.removeEventListener('mouseup',onUp);
    window.removeEventListener('touchmove',onMove);window.removeEventListener('touchend',onUp);
    autoSave();
    if(typeof checkOverflow==='function')checkOverflow();
  };
  handle.addEventListener('mousedown',onDown);
  handle.addEventListener('touchstart',onDown,{passive:false});
}
window.initGapHandle=initGapHandle;

function _bindResizeHandle(handle, role){
  // role: 'bottom' (mexe imgH) | 'top' (mexe imgMarginTop, deixa imagem subir)
  const cont=document.getElementById('imgContainer');
  if(!handle||!cont||handle._bound)return;
  handle._bound=true;
  let resizing=false,startY=0,startH=0,startMt=0,rafPending=false;
  const onDown=(e)=>{
    e.preventDefault();e.stopPropagation();
    resizing=true;
    const pt=e.touches?e.touches[0]:e;
    startY=pt.clientY;
    startH=cont.offsetHeight;
    startMt=slides[cur].imgMarginTop||0;
    document.body.style.cursor='ns-resize';
    window.addEventListener('mousemove',onMove);window.addEventListener('mouseup',onUp);
    window.addEventListener('touchmove',onMove,{passive:false});window.addEventListener('touchend',onUp);
  };
  const onMove=(e)=>{
    if(!resizing)return;
    e.preventDefault();
    const pt=e.touches?e.touches[0]:e;
    const cy=pt.clientY;
    if(rafPending)return;
    rafPending=true;
    requestAnimationFrame(()=>{
      rafPending=false;
      const delta=cy-startY;
      const s=slides[cur];
      if(role==='bottom'){
        s.imgH=Math.round(Math.max(80,Math.min(900,startH+delta)));
      }else{
        const newMt=Math.round(Math.max(-100,Math.min(40,startMt+delta)));
        const dMt=newMt-startMt;
        s.imgMarginTop=newMt;
        s.imgH=Math.round(Math.max(80,Math.min(900,startH-dMt)));
      }
      _applyImgFraming(s);
      applyBgSize(s);
    });
  };
  const onUp=()=>{
    if(!resizing)return;
    resizing=false;
    document.body.style.cursor='';
    window.removeEventListener('mousemove',onMove);window.removeEventListener('mouseup',onUp);
    window.removeEventListener('touchmove',onMove);window.removeEventListener('touchend',onUp);
    updateImgCtrlUI(slides[cur]);autoSave();
    if(typeof checkOverflow==='function')checkOverflow();
  };
  handle.addEventListener('mousedown',onDown);
  handle.addEventListener('touchstart',onDown,{passive:false});
}
function initResizeHandle(){
  _bindResizeHandle(document.getElementById('imgResizeHandle'), 'bottom');
  _bindResizeHandle(document.getElementById('imgResizeHandleTop'), 'top');
}

/* ── Mouse wheel zoom (Canva-style) ── */
function initWheelZoom(){
  const cont=document.getElementById('imgContainer');
  if(!cont||cont._wheelBound)return;
  cont._wheelBound=true;
  cont.addEventListener('wheel',function(e){
    const s=slides[cur];
    if(!s||!s.image)return;
    e.preventDefault();
    const cur_z=s.zoom||1;
    // wheel positivo = zoom out, negativo = zoom in (compativel com pinch)
    const delta=-e.deltaY*0.0015;
    const new_z=Math.max(0.5,Math.min(4,cur_z+delta*cur_z));
    s.zoom=Math.round(new_z*100)/100;
    if(getEffectiveFit(s)==='contain')s.fit='cover'; // wheel zoom assume cover
    applyBgSize(s);updateImgCtrlUI(s);
    clearTimeout(cont._wheelSaveT);
    cont._wheelSaveT=setTimeout(()=>autoSave(),300);
  },{passive:false});
}

/* ── Pinch zoom mobile (2 dedos) ── */
function initPinchZoom(){
  const cont=document.getElementById('imgContainer');
  if(!cont||cont._pinchBound)return;
  cont._pinchBound=true;
  let startDist=0,startZoom=1;
  cont.addEventListener('touchstart',function(e){
    if(e.touches.length===2){
      e.preventDefault();
      const dx=e.touches[0].clientX-e.touches[1].clientX;
      const dy=e.touches[0].clientY-e.touches[1].clientY;
      startDist=Math.hypot(dx,dy);
      startZoom=slides[cur].zoom||1;
    }
  },{passive:false});
  cont.addEventListener('touchmove',function(e){
    if(e.touches.length===2&&startDist>0){
      e.preventDefault();
      const dx=e.touches[0].clientX-e.touches[1].clientX;
      const dy=e.touches[0].clientY-e.touches[1].clientY;
      const dist=Math.hypot(dx,dy);
      const ratio=dist/startDist;
      const new_z=Math.max(0.5,Math.min(4,startZoom*ratio));
      const s=slides[cur];
      s.zoom=Math.round(new_z*100)/100;
      if(getEffectiveFit(s)==='contain')s.fit='cover';
      applyBgSize(s);updateImgCtrlUI(s);
    }
  },{passive:false});
  cont.addEventListener('touchend',function(e){
    if(e.touches.length<2&&startDist>0){
      startDist=0;
      autoSave();
    }
  });
}

/* ── Setas do teclado pra mover imagem (Canva-style) ──
   Em cover: ox/oy 0-100, Shift=1, Alt=10
   Em free: freeX/Y em pixels, Shift=2px, default=10px, Alt=50px */
let _kbBound = false;
function initKeyboardImg(){
  if(_kbBound)return;
  _kbBound=true;
  document.addEventListener('keydown',function(e){
    const tag=(document.activeElement&&document.activeElement.tagName)||'';
    if(tag==='TEXTAREA'||tag==='INPUT'||tag==='SELECT')return;
    const s=slides[cur];
    if(!s||!s.image)return;
    const fit=getEffectiveFit(s);
    if(fit==='contain')return;
    let mexeu=false;
    if(fit==='free'){
      // Pixels — passo padrao 10, Shift=2 (fino), Alt=50 (grosso)
      let step=10; if(e.shiftKey)step=2; else if(e.altKey)step=50;
      if(s.freeX==null||s.freeY==null){applyBgSize(s);} // forca init
      if(e.key==='ArrowLeft'){s.freeX=(s.freeX||0)-step;mexeu=true;}
      else if(e.key==='ArrowRight'){s.freeX=(s.freeX||0)+step;mexeu=true;}
      else if(e.key==='ArrowUp'){s.freeY=(s.freeY||0)-step;mexeu=true;}
      else if(e.key==='ArrowDown'){s.freeY=(s.freeY||0)+step;mexeu=true;}
    }else{
      let step=5; if(e.shiftKey)step=1; else if(e.altKey)step=10;
      if(e.key==='ArrowLeft'){s.ox=Math.max(0,Math.min(100,(s.ox||50)-step));mexeu=true;}
      else if(e.key==='ArrowRight'){s.ox=Math.max(0,Math.min(100,(s.ox||50)+step));mexeu=true;}
      else if(e.key==='ArrowUp'){s.oy=Math.max(0,Math.min(100,(s.oy||50)-step));mexeu=true;}
      else if(e.key==='ArrowDown'){s.oy=Math.max(0,Math.min(100,(s.oy||50)+step));mexeu=true;}
    }
    if(mexeu){
      e.preventDefault();
      applyBgSize(s);
      updateImgCtrlUI(s);
      autoSave();
    }
  });
}

/* ── Modo edicao livre estilo Canva ──
   Pixels absolutos: imagem pode sair do container (cropped por overflow).
   Toggle troca s.fit entre 'cover' e 'free'. Quando volta pra cover,
   preserva ox/oy original (que nunca foram tocados em modo free). */
function toggleFreeEdit(){
  const s=slides[cur];
  if(!s||!s.image)return;
  const cont=document.getElementById('imgContainer');
  if(!cont)return;
  const ativando=getEffectiveFit(s)!=='free';
  if(ativando){
    s.fit='free';
    // freeX/Y serao calculados no proximo applyBgSize a partir de ox/oy
    s.freeX=null;s.freeY=null;
    cont.classList.add('free-edit');
    if(typeof setStatus==='function')setStatus('Modo livre — arraste sem limites, scroll = zoom, setas = ajuste');
  }else{
    s.fit='cover';
    cont.classList.remove('free-edit');
    if(typeof setStatus==='function')setStatus('Voltou pro modo Preencher');
  }
  const btn=document.getElementById('btnFreeEdit');
  if(btn){
    btn.classList.toggle('active',ativando);
    btn.textContent=ativando?'Sair do livre':'Editar livre';
  }
  applyBgSize(s);
  updateImgCtrlUI(s);
  autoSave();
  setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},4000);
}
window.toggleFreeEdit=toggleFreeEdit;

/* ── Reordenar slides via drag and drop nos top-dots (Canva-style) ──
   Usuario arrasta um dot pra outra posicao e o array de slides eh
   reordenado. Mantem o slide atual na posicao "que foi movida". */
let _dragIdx = -1;
function _bindDotsDragAndDrop(){
  const td=document.getElementById('topDots');
  if(!td)return;
  td.querySelectorAll('.top-dot').forEach(dot=>{
    dot.addEventListener('dragstart',(e)=>{
      _dragIdx=parseInt(dot.dataset.idx,10);
      dot.style.opacity='0.4';
      try{e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',_dragIdx);}catch(err){}
    });
    dot.addEventListener('dragend',()=>{
      dot.style.opacity='';
      _dragIdx=-1;
      td.querySelectorAll('.top-dot').forEach(d=>d.style.boxShadow='');
    });
    dot.addEventListener('dragover',(e)=>{
      e.preventDefault();
      try{e.dataTransfer.dropEffect='move';}catch(err){}
      td.querySelectorAll('.top-dot').forEach(d=>d.style.boxShadow='');
      dot.style.boxShadow='0 0 0 2px #1d9bf0';
    });
    dot.addEventListener('dragleave',()=>{
      dot.style.boxShadow='';
    });
    dot.addEventListener('drop',(e)=>{
      e.preventDefault();
      const fromIdx=_dragIdx;
      const toIdx=parseInt(dot.dataset.idx,10);
      td.querySelectorAll('.top-dot').forEach(d=>d.style.boxShadow='');
      if(fromIdx<0||toIdx<0||fromIdx===toIdx)return;
      // Reordena: tira do fromIdx, insere no toIdx
      const moved=slides.splice(fromIdx,1)[0];
      slides.splice(toIdx,0,moved);
      // Ajusta cur pra continuar mostrando o slide que estava ativo
      if(cur===fromIdx){
        cur=toIdx;
      }else if(fromIdx<cur && toIdx>=cur){
        cur=cur-1;
      }else if(fromIdx>cur && toIdx<=cur){
        cur=cur+1;
      }
      render();
      autoSave();
      if(typeof setStatus==='function')setStatus('✓ Slide movido: posição '+(fromIdx+1)+' → '+(toIdx+1));
      setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},2500);
    });
  });
}

/* ── Navigation ── */
function goTo(i){cur=i;editingText=false;showImgCtrl=false;document.getElementById('imgCtrlPanel').style.display='none';render();}
function navigate(d){goTo(Math.max(0,Math.min(slides.length-1,cur+d)));}

/* ── Profile ── */
function toggleProfileEdit(){
  showProfileEdit=!showProfileEdit;
  document.getElementById('profileEditPanel').style.display=showProfileEdit?'flex':'none';
  if(showProfileEdit){document.getElementById('inputName').value=profile.name;document.getElementById('inputHandle').value=profile.handle;}
}
function updateProfile(){
  profile.name=document.getElementById('inputName').value;
  profile.handle=document.getElementById('inputHandle').value;
  document.getElementById('dispName').textContent=profile.name;
  document.getElementById('dispHandle').textContent=profile.handle;
  autoSave();
}
function onAvatarFile(input){
  const f=input.files[0];if(!f)return;
  const r=new FileReader();
  r.onload=e=>{avatarDataUrl=e.target.result;render();autoSave();};
  r.readAsDataURL(f);input.value='';
}

/* ── Text editing ── */
function startEdit(){editingText=true;render();setTimeout(()=>{const ta=document.getElementById('editTA');if(ta){ta.focus();_updateCharCount(ta.value);}},10);}

/* Contador de caracteres ao vivo com cores semaforicas:
   - verde:  < 280 (ideal)
   - laranja: 280-380 (limite confortavel)
   - vermelho: > 380 (perto do max 420)
   - vermelho forte: > 420 (vai estourar) */
function _updateCharCount(value){
  const el=document.getElementById('charCount');
  if(!el)return;
  const n=(value||'').length;
  let color='#10b981'; // verde
  let label=n+' / 420';
  if(n>420){color='#dc2626';label=n+' / 420 ⚠ MAX';} // vermelho forte
  else if(n>380){color='#ea580c';label=n+' / 420';}    // laranja
  else if(n>280){color='#f59e0b';label=n+' / 420';}    // amarelo
  el.textContent=label;
  el.style.color=color;
}
window._updateCharCount=_updateCharCount;
function liveUpdate(val){slides[cur].text=val;document.getElementById('textDisplay').innerHTML=textToDisplayHTML(val);}
/* Insere um topico (bullet "• ") na posicao do cursor do textarea.
   - Se o cursor esta numa linha vazia, poe "• " no inicio dela.
   - Se a linha ja tem conteudo, quebra linha e adiciona "• " (novo topico).
   Atualiza preview, contador e autosave em seguida. */
function inserirBullet(){
  const ta=document.getElementById('editTA');
  if(!ta)return;
  const start=ta.selectionStart;
  const val=ta.value;
  const lineStart=val.lastIndexOf('\n',start-1)+1;
  const antesNaLinha=val.slice(lineStart,start);
  let novoVal,newPos;
  if(antesNaLinha.trim()===''){
    // linha vazia: poe "• " no inicio (sem criar linha nova)
    novoVal=val.slice(0,lineStart)+'• '+val.slice(lineStart);
    newPos=lineStart+2;
  }else{
    // linha com conteudo: quebra e adiciona novo topico
    novoVal=val.slice(0,start)+'\n• '+val.slice(start);
    newPos=start+3;
  }
  ta.value=novoVal;
  ta.focus();
  ta.setSelectionRange(newPos,newPos);
  if(typeof _updateCharCount==='function')_updateCharCount(ta.value);
  if(typeof liveUpdate==='function')liveUpdate(ta.value);
  if(typeof autoSave==='function')autoSave();
}
window.inserirBullet=inserirBullet;
function saveEdit(){slides[cur].text=document.getElementById('editTA').value;editingText=false;render();autoSave();}

/* ── Garante elementos do edit area em posts ANTIGOS ──
   Posts gerados antes do template ter btn-revisar-pt + char-count + lang
   nao tem esses elementos no HTML. Injeta dinamicamente. */
function _ensureEditButtons(){
  const ta=document.getElementById('editTA');
  if(ta){
    // Spellcheck nativo e lang pt-BR no textarea
    if(ta.getAttribute('lang')!=='pt-BR')ta.setAttribute('lang','pt-BR');
    if(ta.getAttribute('spellcheck')!=='true')ta.setAttribute('spellcheck','true');
  }
  const actions=document.querySelector('#textEditArea .edit-actions');
  if(!actions)return;
  // SVG do botao Revisar PT — Heroicons "language" icon
  const SVG_REVISAR=
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    +'<path d="M3 5h12M9 3v2m1.048 9.5A18.022 18.022 0 016.412 9m6.088 9h7M11 21l5-10 5 10M12.751 5C11.783 10.77 8.07 15.61 3 18.129"/>'
    +'</svg>'
    +'<span class="label">Revisar PT</span>';
  // Botao Revisar PT — injeta se nao existir, ou MIGRA se ja existir
  // com formato antigo (emoji 📝 estatico). Posts gerados antes da
  // migracao pra SVG nao tem o markup novo no HTML.
  let btnRev=actions.querySelector('.btn-revisar-pt');
  if(!btnRev){
    btnRev=document.createElement('button');
    btnRev.type='button';
    btnRev.className='btn-revisar-pt';
    btnRev.title='Verifica ortografia e gramática via LanguageTool';
    btnRev.onclick=function(){revisarPortugues();};
    btnRev.innerHTML=SVG_REVISAR;
    const save=actions.querySelector('.btn-save');
    if(save&&save.nextSibling)actions.insertBefore(btnRev,save.nextSibling);
    else if(save)save.parentNode.appendChild(btnRev);
    else actions.appendChild(btnRev);
  } else if(!btnRev.querySelector('svg')){
    // Migra botao legado (texto "📝 Revisar PT") pro novo SVG
    btnRev.innerHTML=SVG_REVISAR;
  }
  // Botao Topico/Bullet — insere "• " no cursor. Fica logo apos o Salvar
  // (afterend) pra ficar agrupado com as acoes de edicao de texto.
  if(!actions.querySelector('.btn-bullet')){
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='btn-bullet';
    btn.title='Insere um tópico (bullet point) na posição do cursor';
    btn.onclick=function(){inserirBullet();};
    btn.innerHTML=
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      +'<circle cx="4.5" cy="7" r="1.4" fill="currentColor" stroke="none"/>'
      +'<circle cx="4.5" cy="12" r="1.4" fill="currentColor" stroke="none"/>'
      +'<circle cx="4.5" cy="17" r="1.4" fill="currentColor" stroke="none"/>'
      +'<line x1="9" y1="7" x2="20" y2="7"/><line x1="9" y1="12" x2="20" y2="12"/><line x1="9" y1="17" x2="20" y2="17"/>'
      +'</svg>'
      +'<span class="label">Tópico</span>';
    const save=actions.querySelector('.btn-save');
    if(save)save.insertAdjacentElement('afterend',btn);
    else actions.appendChild(btn);
  }
  // Botao Polir — SVG "sparkles" (Heroicons) + label.
  if(!actions.querySelector('.btn-polir')){
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='btn-polir';
    btn.title='Reescreve o slide com Claude removendo vícios de IA e simplificando linguagem';
    btn.onclick=function(){polirSlide();};
    btn.innerHTML=
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      +'<path d="M5 3v4M3 5h4M6 17v4m-2-2h4m5-16l2.286 6.857L21 12l-5.714 2.143L13 21l-2.286-6.857L5 12l5.714-2.143L13 3z"/>'
      +'</svg>'
      +'<span class="label">Polir</span>';
    const revisar=actions.querySelector('.btn-revisar-pt');
    if(revisar&&revisar.nextSibling)actions.insertBefore(btn,revisar.nextSibling);
    else actions.appendChild(btn);
  }
  // Botao Marcar revisao — toggle verde/vermelho do chip do slide atual.
  // SVG inline em vez de unicode pra renderizar igual em qualquer fonte.
  // ico-empty = circulo vazio (pendente), ico-check = circulo preenchido
  // com check dentro (revisado). CSS toggle via .is-reviewed mostra um ou outro.
  if(!actions.querySelector('.btn-mark-reviewed')){
    const btn=document.createElement('button');
    btn.type='button';
    btn.className='btn-mark-reviewed';
    btn.title='Marca esse slide como revisado. O chip fica verde no topo. Se editar o texto depois, o status reseta automaticamente.';
    btn.onclick=function(){toggleSlideReviewed();};
    btn.innerHTML=
      '<svg class="ico-empty" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
      +'<circle cx="12" cy="12" r="9"/>'
      +'</svg>'
      +'<svg class="ico-check" width="13" height="13" viewBox="0 0 24 24" aria-hidden="true">'
      +'<circle cx="12" cy="12" r="10" fill="currentColor"/>'
      +'<path d="M8 12.5 L11 15.5 L16.5 9.5" stroke="#fff" stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
      +'</svg>'
      +'<span class="label">Marcar revisado</span>';
    const polir=actions.querySelector('.btn-polir');
    if(polir&&polir.nextSibling)actions.insertBefore(btn,polir.nextSibling);
    else actions.appendChild(btn);
  }
  _updateMarkReviewedBtn();
  // Contador de chars — injeta se nao existir
  if(!actions.querySelector('#charCount')){
    const cc=document.createElement('span');
    cc.id='charCount';
    cc.className='char-count';
    cc.style.cssText='margin-left:auto;font-size:11px;font-weight:700;color:#10b981';
    cc.textContent='0 / 420';
    actions.appendChild(cc);
    if(ta)_updateCharCount(ta.value);
  }
  // Adiciona handler de input pra atualizar contador
  if(ta && !ta._editHandlersBound){
    ta._editHandlersBound=true;
    ta.addEventListener('input',function(){
      _updateCharCount(this.value);
      if(typeof liveUpdate==='function')liveUpdate(this.value);
      if(typeof autoSave==='function')autoSave();
    });
  }
}

/* Atualiza estado visual do botao Marcar como revisado.
   Chamado pelo _ensureEditButtons (que roda apos cada entrada em
   edit mode). Reflete o status atual do slide via hash.
   Os SVGs (ico-empty/ico-check) ficam fixos no DOM — o toggle eh
   feito por CSS via .is-reviewed. Aqui so atualizamos label e classe. */
function _updateMarkReviewedBtn(){
  const btn=document.querySelector('#textEditArea .btn-mark-reviewed');
  if(!btn)return;
  const label=btn.querySelector('.label');
  const rev=isSlideReviewed(cur);
  if(rev){
    btn.classList.add('is-reviewed');
    if(label)label.textContent='Revisado';
    btn.title='Slide marcado como revisado. Clique pra desmarcar.';
  } else {
    btn.classList.remove('is-reviewed');
    if(label)label.textContent='Marcar revisado';
    btn.title='Marca esse slide como revisado. Chip fica verde. Se editar o texto, reseta sozinho.';
  }
}

/* ── Polir slide com Claude (remove vicios + simplifica linguagem) ── */
async function polirSlide(){
  const ta=document.getElementById('editTA');
  if(!ta)return;
  const text=ta.value.trim();
  if(!text)return;
  const sendToParent = window.parent && window.parent !== window;
  if(sendToParent){
    try{window.parent.postMessage({type:'bearlz-polir-start',text:text},'*');}catch(e){}
  }
  try{
    const r=await fetch('/api/polir-slide',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:text})
    });
    const d=await r.json();
    if(sendToParent){
      try{window.parent.postMessage({type:'bearlz-polir-result',data:d,original:text},'*');}catch(e){}
    } else {
      if(d.ok && d.texto_novo){
        if(confirm('Aplicar versao polida?\n\nANTES:\n'+text+'\n\nDEPOIS:\n'+d.texto_novo)){
          ta.value=d.texto_novo;
          _updateCharCount(ta.value);
          if(typeof liveUpdate==='function')liveUpdate(ta.value);
          if(typeof autoSave==='function')autoSave();
        }
      } else {
        alert('Erro: '+(d.error||'desconhecido'));
      }
    }
  }catch(e){
    if(sendToParent){
      try{window.parent.postMessage({type:'bearlz-polir-result',data:{error:e.message}},'*');}catch(_){}
    }
  }
}
window.polirSlide=polirSlide;

// Listener: parent posta texto polido aprovado
window.addEventListener('message',function(e){
  if(!e.data||typeof e.data!=='object')return;
  if(e.data.type==='bearlz-polir-apply' && typeof e.data.text==='string'){
    const ta=document.getElementById('editTA');
    if(ta){
      ta.value=e.data.text;
      _updateCharCount(ta.value);
      if(typeof liveUpdate==='function')liveUpdate(ta.value);
      if(typeof autoSave==='function')autoSave();
    }
  } else if(e.data.type==='bearlz-polir-retry'){
    // Parent pediu pra tentar polir de novo (apos erro de parsing).
    // Re-dispara polirSlide() — vai chamar /api/polir-slide novamente.
    if(typeof polirSlide==='function')polirSlide();
  }
});

/* ── Revisor de portugues (LanguageTool) ──
   Verifica erros ortograficos e gramaticais no texto do textarea. */
async function revisarPortugues(){
  const ta=document.getElementById('editTA');
  if(!ta){return;}
  const text=ta.value.trim();
  if(!text){return;}
  // Renderiza modal de loading no parent (iframe) pra ter espaco
  const sendToParent = window.parent && window.parent !== window;
  if(sendToParent){
    try{
      window.parent.postMessage({type:'bearlz-revisar-pt-start',text:text},'*');
    }catch(e){}
  }
  // Chama backend
  try{
    const r=await fetch('/api/check-pt',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:text})
    });
    const d=await r.json();
    if(sendToParent){
      try{
        window.parent.postMessage({type:'bearlz-revisar-pt-result',data:d,text:text},'*');
      }catch(e){}
    } else {
      _showRevisarLocal(d,text);
    }
  }catch(e){
    if(sendToParent){
      try{ window.parent.postMessage({type:'bearlz-revisar-pt-result',data:{error:e.message},text:text},'*'); }catch(_){}
    }
  }
}
window.revisarPortugues=revisarPortugues;

// Listener: parent posta correcao aplicada
window.addEventListener('message',function(e){
  if(!e.data||typeof e.data!=='object')return;
  if(e.data.type==='bearlz-revisar-pt-apply' && typeof e.data.text==='string'){
    const ta=document.getElementById('editTA');
    if(ta){
      ta.value=e.data.text;
      _updateCharCount(ta.value);
      if(typeof liveUpdate==='function')liveUpdate(ta.value);
      if(typeof autoSave==='function')autoSave();
    }
  }
});

// Fallback local (sem iframe) — modal embutido simples
function _showRevisarLocal(d,text){
  let m=document.getElementById('revisarPtModal');
  if(!m){
    m=document.createElement('div');
    m.id='revisarPtModal';
    m.style.cssText='display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:99999;padding:24px;overflow-y:auto';
    m.onclick=(e)=>{if(e.target===m)m.style.display='none';};
    document.body.appendChild(m);
  }
  const matches=(d&&d.matches)||[];
  m.innerHTML=`<div style="background:#fff;border-radius:12px;max-width:640px;width:100%;margin:0 auto;padding:18px;font-family:'Open Sans',sans-serif">
    <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #eee;padding-bottom:8px;margin-bottom:12px">
      <strong>📝 Revisão PT — ${matches.length} ${matches.length===1?'erro':'erros'}</strong>
      <button onclick="document.getElementById('revisarPtModal').style.display='none'" style="background:none;border:none;font-size:22px;cursor:pointer">×</button>
    </div>
    ${matches.length===0?'<p style="color:#10b981;padding:20px;text-align:center">✓ Nenhum erro encontrado</p>':matches.map(em=>`
      <div style="border:1px solid #fee2e2;background:#fef2f2;border-radius:8px;padding:10px;margin-bottom:8px">
        <div style="font-size:12px;color:#991b1b;font-weight:600">${em.message}</div>
        ${em.suggestions.length?`<div style="margin-top:4px;font-size:11px">Sugestões: ${em.suggestions.map(s=>`<code style="background:#dbeafe;color:#1e3a8a;padding:2px 6px;border-radius:4px;margin-right:4px">${s}</code>`).join('')}</div>`:''}
      </div>
    `).join('')}
  </div>`;
  m.style.display='block';
}

/* ── Image ── */
function onImgFile(input){
  const f=input.files[0];if(!f)return;
  const r=new FileReader();
  r.onload=e=>{
    const tmp=new Image();
    tmp.onload=()=>{slides[cur].image=e.target.result;slides[cur].zoom=1;slides[cur].ox=50;slides[cur].oy=50;slides[cur].imgH=null;slides[cur].fit='cover';slides[cur].imgNW=tmp.naturalWidth;slides[cur].imgNH=tmp.naturalHeight;render();autoSave();};
    tmp.src=e.target.result;
  };
  r.readAsDataURL(f);input.value='';
}
function clearImage(){
  const s=slides[cur];
  s.image=null;s.video=null;s.zoom=1;s.ox=50;s.oy=50;
  s.imgH=null;s.fit=null;s.imgNW=null;s.imgNH=null;
  s.freeX=null;s.freeY=null;s.imgMarginTop=null;s.gapTextImg=null;
  // Reseta margin-top do .img-section + gap handle pro proximo render limpo
  const sec=document.querySelector('.img-section');
  if(sec)sec.style.marginTop='';
  const gap=document.getElementById('gapHandle');
  if(gap)gap.style.height='';
  const panel=document.getElementById('imgCtrlPanel');
  if(panel)panel.style.display='none';
  render();autoSave();
}
function toggleImgCtrl(){const panel=document.getElementById('imgCtrlPanel');const showing=panel.style.display!=='none';panel.style.display=showing?'none':'block';}
// applyBgSize: nome historico, hoje aplica posicao+tamanho no <img> real
// dentro de #imgContainer. Tres modos: cover (constrained crop), contain
// (letterbox), free (pixels absolutos, pode sair do container).
function applyBgSize(s){
  const cont=document.getElementById('imgContainer');
  const img =document.getElementById('imgReal');
  if(!cont||!img)return;
  // Carrega dimensoes naturais se ainda nao temos
  if(!s.imgNW||!s.imgNH){
    if(img.naturalWidth&&img.naturalHeight){
      s.imgNW=img.naturalWidth;s.imgNH=img.naturalHeight;
    }else{
      img.addEventListener('load',()=>{
        s.imgNW=img.naturalWidth;s.imgNH=img.naturalHeight;applyBgSize(s);
      },{once:true});
      return;
    }
  }
  const cw=cont.offsetWidth,ch=cont.offsetHeight;if(!cw||!ch)return;
  const z=s.zoom||1;
  const fit=getEffectiveFit(s);
  if(fit==='contain'){
    const scale=Math.min(cw/s.imgNW,ch/s.imgNH);
    const fw=s.imgNW*scale,fh=s.imgNH*scale;
    img.style.left=((cw-fw)/2)+'px';
    img.style.top =((ch-fh)/2)+'px';
    img.style.width =fw+'px';
    img.style.height=fh+'px';
  }else if(fit==='free'){
    // Modo livre estilo Canva: pixels absolutos, sem clamp.
    // Inicia com tamanho "cover" pra ficar igual ao que o usuario vinha
    // editando, mas dali em diante o user controla por px direto.
    const baseFree=Math.max(cw/s.imgNW,ch/s.imgNH);
    const fw=s.imgNW*baseFree*z,fh=s.imgNH*baseFree*z;
    // Se ainda nao tem freeX/Y, deriva do ox/oy atual pra continuar
    // de onde estava (transicao suave de cover -> free)
    if(s.freeX==null){
      const ox=s.ox!=null?s.ox:50;
      s.freeX=(ox/100)*(cw-fw);
    }
    if(s.freeY==null){
      const oy=s.oy!=null?s.oy:50;
      s.freeY=(oy/100)*(ch-fh);
    }
    img.style.left=s.freeX+'px';
    img.style.top =s.freeY+'px';
    img.style.width =fw+'px';
    img.style.height=fh+'px';
  }else{
    const base=Math.max(cw/s.imgNW,ch/s.imgNH);
    const fw=s.imgNW*base*z,fh=s.imgNH*base*z;
    const ox=s.ox||50,oy=s.oy||50;
    const dx=(ox/100)*(cw-fw),dy=(oy/100)*(ch-fh);
    img.style.left=dx+'px';
    img.style.top =dy+'px';
    img.style.width =fw+'px';
    img.style.height=fh+'px';
  }
}
function updateZoom(val){slides[cur].zoom=parseFloat(val);document.getElementById('valZoom').textContent=Math.round(val*100)+'%';applyBgSize(slides[cur]);updateImgCtrlUI(slides[cur]);autoSave();}
function updateOx(val){slides[cur].ox=parseInt(val);document.getElementById('valOx').textContent=val+'%';applyBgSize(slides[cur]);updateImgCtrlUI(slides[cur]);autoSave();}
function updateOy(val){slides[cur].oy=parseInt(val);document.getElementById('valOy').textContent=val+'%';applyBgSize(slides[cur]);updateImgCtrlUI(slides[cur]);autoSave();}
function updateImgH(val){slides[cur].imgH=parseInt(val);document.getElementById('valH').textContent=val+'px';const cont=document.getElementById('imgContainer');if(cont){cont.style.height=val+'px';cont.style.flex='none';}applyBgSize(slides[cur]);updateImgCtrlUI(slides[cur]);autoSave();}
function resetImgCtrl(){
  slides[cur].zoom=1;slides[cur].ox=50;slides[cur].oy=50;slides[cur].imgH=null;slides[cur].fit=null;
  renderImgSection();
  autoSave();
}

/* ── Add / Remove slides ── */
function addSlide(){
  const nid=Math.max(...slides.map(x=>x.id))+1;
  slides.splice(cur+1,0,{
    id:nid,
    text:`Novo slide. Use **negrito** para destacar.`,
    image:null,zoom:1,ox:50,oy:50,
    imgH:null,fit:null,imgNW:null,imgNH:null
  });
  cur++;editingText=true;render();autoSave();
  setTimeout(()=>document.getElementById('editTA').focus(),10);
}
function removeSlide(){if(slides.length<=1)return;slides.splice(cur,1);cur=Math.min(cur,slides.length-1);editingText=false;render();autoSave();}

/* ── Canvas export engine ── */
function loadImg(src){
  return new Promise((res,rej)=>{
    const img=new Image();
    // data: URIs nao precisam de crossOrigin (evita bloqueio em SVGs)
    if(!src.startsWith('data:'))img.crossOrigin='anonymous';
    img.onload=()=>res(img);
    img.onerror=rej;
    img.src=src;
  });
}
function roundRect(ctx,x,y,w,h,r){
  ctx.beginPath();ctx.moveTo(x+r,y);ctx.lineTo(x+w-r,y);ctx.quadraticCurveTo(x+w,y,x+w,y+r);ctx.lineTo(x+w,y+h-r);ctx.quadraticCurveTo(x+w,y+h,x+w-r,y+h);ctx.lineTo(x+r,y+h);ctx.quadraticCurveTo(x,y+h,x,y+h-r);ctx.lineTo(x,y+r);ctx.quadraticCurveTo(x,y,x+r,y);ctx.closePath();
}
function canvasWrapText(ctx,segs,maxW,fSize){
  const lines=[];let line=[],lineW=0;
  let prevSpaced=false;
  for(const seg of segs){
    const words=seg.t.split(' ');
    let first=true;
    for(let i=0;i<words.length;i++){
      const w=words[i];
      if(!w){prevSpaced=true;continue;}
      ctx.font=`${seg.b?'700':'400'} ${fSize}px Open Sans,sans-serif`;
      const sp=lineW>0&&(!first||prevSpaced);
      const pre=sp?' ':'';
      const tw=ctx.measureText(pre+w).width;
      if(lineW>0&&lineW+tw>maxW){lines.push(line);line=[];lineW=0;prevSpaced=false;first=true;}
      const sp2=lineW>0&&(!first||prevSpaced);
      const pre2=sp2?' ':'';
      const tw2=ctx.measureText(pre2+w).width;
      const existing=line.find(c=>c.bold===seg.b);
      if(existing&&line[line.length-1]===existing){existing.text+=pre2+w;}
      else{line.push({text:pre2+w,bold:seg.b});}
      lineW+=tw2;first=false;prevSpaced=false;
    }
  }
  if(line.length>0)lines.push(line);
  return lines;
}
function canvasAvatarPlaceholder(ctx,x,y,sz){
  ctx.save();ctx.beginPath();ctx.arc(x+sz/2,y+sz/2,sz/2,0,Math.PI*2);ctx.clip();
  const g=ctx.createLinearGradient(x,y,x+sz,y+sz);g.addColorStop(0,'#1a1a2e');g.addColorStop(1,'#16213e');
  ctx.fillStyle=g;ctx.fillRect(x,y,sz,sz);
  ctx.fillStyle='#fff';ctx.font='900 42px Open Sans,sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
  ctx.fillText('G',x+sz/2,y+sz/2);ctx.textAlign='left';ctx.textBaseline='top';ctx.restore();
}
function canvasDrawBadge(ctx,x,y,sz){
  const sc=sz/22;
  ctx.save();ctx.translate(x,y);ctx.scale(sc,sc);
  ctx.beginPath();
  ctx.moveTo(20.396,11);ctx.bezierCurveTo(20.378,10.354,20.181,9.725,19.826,9.184);ctx.bezierCurveTo(19.472,8.644,18.974,8.212,18.388,7.938);ctx.bezierCurveTo(18.611,7.331,18.658,6.674,18.528,6.041);ctx.bezierCurveTo(18.397,5.407,18.091,4.823,17.646,4.354);ctx.bezierCurveTo(17.176,3.909,16.593,3.604,15.959,3.472);ctx.bezierCurveTo(15.326,3.342,14.669,3.389,14.062,3.612);ctx.bezierCurveTo(13.789,3.025,13.358,2.526,12.817,2.172);ctx.bezierCurveTo(12.276,1.818,11.647,1.62,11,1.604);ctx.bezierCurveTo(10.354,1.621,9.727,1.817,9.187,2.172);ctx.bezierCurveTo(8.647,2.527,8.218,3.027,7.947,3.612);ctx.bezierCurveTo(7.339,3.389,6.68,3.34,6.045,3.472);ctx.bezierCurveTo(5.41,3.602,4.825,3.908,4.355,4.354);ctx.bezierCurveTo(3.91,4.824,3.606,5.409,3.477,6.044);ctx.bezierCurveTo(3.347,6.677,3.397,7.334,3.621,7.940);ctx.bezierCurveTo(3.034,8.214,2.534,8.645,2.178,9.185);ctx.bezierCurveTo(1.822,9.725,1.623,10.355,1.604,11.002);ctx.bezierCurveTo(1.624,11.649,1.822,12.278,2.178,12.819);ctx.bezierCurveTo(2.534,13.359,3.034,13.791,3.621,14.064);ctx.bezierCurveTo(3.397,14.67,3.347,15.327,3.477,15.96);ctx.bezierCurveTo(3.607,16.596,3.91,17.181,4.355,17.650);ctx.bezierCurveTo(4.825,18.096,5.41,18.402,6.045,18.533);ctx.bezierCurveTo(6.68,18.663,7.339,18.616,7.947,18.389);ctx.bezierCurveTo(8.218,18.976,8.647,19.476,9.187,19.832);ctx.bezierCurveTo(9.727,20.188,10.354,20.387,11,20.404);ctx.bezierCurveTo(11.646,20.387,12.275,20.185,12.817,19.829);ctx.bezierCurveTo(13.359,19.473,13.789,18.972,14.062,18.385);ctx.bezierCurveTo(14.669,18.612,15.326,18.659,15.959,18.529);ctx.bezierCurveTo(16.593,18.398,17.176,18.094,17.646,17.648);ctx.bezierCurveTo(18.091,17.178,18.397,16.593,18.528,15.957);ctx.bezierCurveTo(18.66,15.324,18.611,14.667,18.388,14.061);ctx.bezierCurveTo(18.975,13.788,19.473,13.356,19.826,12.816);ctx.bezierCurveTo(20.179,12.276,20.376,11.645,20.394,10.998);
  ctx.fillStyle='#1d9bf0';ctx.fill();
  ctx.beginPath();ctx.moveTo(9.585,14.929);ctx.lineTo(6.305,11.649);ctx.lineTo(7.473,10.481);ctx.lineTo(9.585,12.593);ctx.lineTo(14.945,7.233);ctx.lineTo(16.113,8.401);ctx.closePath();
  ctx.fillStyle='#fff';ctx.fill();
  ctx.restore();
}

async function drawSlideToCanvas(canvas,slide){
  await Promise.all([
    document.fonts.load('400 44px Inter'),
    document.fonts.load('700 44px Inter'),
    document.fonts.load('700 46px Inter'),
    document.fonts.load('400 39px Inter'),
  ]);
  const ctx=canvas.getContext('2d');
  canvas.width=W;canvas.height=H;
  const px=57,fSize=44,lh=Math.round(fSize*1.6),nameSize=46,handleSize=39;
  ctx.fillStyle='#ffffff';ctx.fillRect(0,0,W,H);
  let cy=64;
  const picSz=160;

  if(avatarDataUrl){
    try{const img=await loadImg(avatarDataUrl);ctx.save();ctx.beginPath();ctx.arc(px+picSz/2,cy+picSz/2,picSz/2,0,Math.PI*2);ctx.clip();ctx.drawImage(img,px,cy,picSz,picSz);ctx.restore();}
    catch(e){canvasAvatarPlaceholder(ctx,px,cy,picSz);}
  }else{canvasAvatarPlaceholder(ctx,px,cy,picSz);}

  const tx=px+picSz+24;
  ctx.textBaseline='top';
  const nameHandleH=nameSize+8+handleSize;
  const nameY=cy+Math.round((picSz-nameHandleH)/2);
  ctx.fillStyle='#0f1419';ctx.font=`700 ${nameSize}px Open Sans,sans-serif`;
  ctx.fillText(profile.name,tx,nameY);
  ctx.fillStyle='#555555';ctx.font=`400 ${handleSize}px Open Sans,sans-serif`;
  ctx.fillText(profile.handle,tx,nameY+nameSize+8);
  const hw=ctx.measureText(profile.handle).width;
  canvasDrawBadge(ctx,tx+hw+10,nameY+nameSize+8,32);
  cy+=picSz+44;

  const maxW=W-px*2;
  const paras=slide.text.split(/\n\n+/);
  for(let pi=0;pi<paras.length;pi++){
    // Dentro do paragrafo, processa cada linha (\n). Linhas que comecam com
    // • ou - viram bullets: marcador na 1a linha + recuo nas continuacoes.
    const rawLines=paras[pi].split('\n');
    for(let li=0;li<rawLines.length;li++){
      let lineTxt=rawLines[li];
      if(!lineTxt.trim()){cy+=Math.round(lh*0.4);continue;}
      const bm=/^\s*[•\-\*]\s+(.*)$/.exec(lineTxt);
      const isBullet=!!bm;
      if(isBullet)lineTxt=bm[1];
      ctx.font=`400 ${fSize}px Open Sans,sans-serif`;
      const indent=isBullet?ctx.measureText('•  ').width:0;
      const segs=parseBold(lineTxt);
      const wrapped=canvasWrapText(ctx,segs,maxW-indent,fSize);
      for(let wi=0;wi<wrapped.length;wi++){
        const ln=wrapped[wi];
        let lx=px+indent;
        if(isBullet&&wi===0){
          ctx.font=`400 ${fSize}px Open Sans,sans-serif`;
          ctx.fillStyle='#0f1419';
          ctx.fillText('•',px,cy);
        }
        for(const chunk of ln){
          ctx.font=`${chunk.bold?'700':'400'} ${fSize}px Open Sans,sans-serif`;
          ctx.fillStyle='#0f1419';
          ctx.fillText(chunk.text,lx,cy);
          lx+=ctx.measureText(chunk.text).width;
        }
        cy+=lh;
      }
    }
    if(pi<paras.length-1)cy+=24;
  }
  cy+=20;

  const maxImgH=H-cy-20;
  const minImgH=Math.min(380,maxImgH);
  const imgH=Math.max(slide.imgH?Math.min(Math.round(slide.imgH*(1080/420)),maxImgH):maxImgH,minImgH);
  const radius=24;
  const isSVGimg=slide.image&&slide.image.startsWith('data:image/svg');
  const fitMode=isSVGimg?'contain':(slide.fit||'cover');
  if(slide.image){
    try{
      const img=await loadImg(slide.image);
      const sw=W-px*2,sh=imgH;
      // Mesma regra de auto-fit do viewer: grafico (pela URL) ou imagem
      // muito larga sem fit manual sai INTEIRA no PNG exportado tambem.
      let fmEff=fitMode;
      const isChartUrl=/\/static\/charts\/|fredgraph|stooq\.com|quickchart/.test(slide.image||'');
      if(!slide.fit&&!isSVGimg&&(isChartUrl||(img.width&&img.height&&img.width/img.height>=1.9)))fmEff='contain';
      ctx.save();roundRect(ctx,px,cy,sw,sh,radius);ctx.clip();
      if(fmEff==='contain'){
        // Letterbox: show full image with light background
        ctx.fillStyle='#ffffff';ctx.fillRect(px,cy,sw,sh);
        const scale=Math.min(sw/img.width,sh/img.height);
        const fw=img.width*scale,fh=img.height*scale;
        const ix=px+(sw-fw)/2,iy=cy+(sh-fh)/2;
        ctx.drawImage(img,ix,iy,fw,fh);
      }else{
        // Cover: fill and crop
        const z=slide.zoom||1;
        const base=Math.max(sw/img.width,sh/img.height);
        const fw=img.width*base*z,fh=img.height*base*z;
        const dx=px+((slide.ox||50)/100)*(sw-fw);
        const dy=cy+((slide.oy||50)/100)*(sh-fh);
        ctx.drawImage(img,dx,dy,fw,fh);
      }
      ctx.restore();
    }catch(e){
      ctx.save();roundRect(ctx,px,cy,W-px*2,imgH,radius);ctx.clip();
      ctx.fillStyle='#f4f4f4';ctx.fillRect(px,cy,W-px*2,imgH);
      ctx.fillStyle='#ccc';ctx.font='400 36px Open Sans,sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
      ctx.fillText(`Slide ${slide.id}`,W/2,cy+imgH/2);ctx.textAlign='left';ctx.textBaseline='top';ctx.restore();
    }
  }else{
    ctx.save();roundRect(ctx,px,cy,W-px*2,imgH,radius);ctx.clip();
    ctx.fillStyle='#f4f4f4';ctx.fillRect(px,cy,W-px*2,imgH);
    ctx.fillStyle='#ccc';ctx.font='400 36px Open Sans,sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';
    ctx.fillText(`Slide ${slide.id}`,W/2,cy+imgH/2);ctx.textAlign='left';ctx.textBaseline='top';ctx.restore();
  }
}

function setStatus(msg){document.getElementById('dlStatus').textContent=msg;}

async function preloadImages(){
  const urls=slides.map(s=>s.image).filter(u=>u&&!u.startsWith('data:'));
  if(avatarDataUrl&&!avatarDataUrl.startsWith('data:'))urls.push(avatarDataUrl);
  await Promise.all(urls.map(u=>new Promise(res=>{
    const img=new Image();img.crossOrigin='anonymous';
    img.onload=res;img.onerror=res;img.src=u;
  })));
}



// ===== Canva-style bold editor =====
function wrapBoldSelection(){
  const ta=document.getElementById('editTA');
  if(!ta)return;
  const start=ta.selectionStart,end=ta.selectionEnd;
  if(start===end){ta.focus();return;}
  const txt=ta.value;
  const selected=txt.slice(start,end);
  // Check if already bold — unwrap
  let newSel,delta;
  if(selected.startsWith('**')&&selected.endsWith('**')&&selected.length>4){
    newSel=selected.slice(2,-2);delta=-4;
  }else{
    newSel='**'+selected+'**';delta=4;
  }
  const newText=txt.slice(0,start)+newSel+txt.slice(end);
  ta.value=newText;
  ta.focus();
  ta.selectionStart=start;
  ta.selectionEnd=end+delta;
  // Live preview update
  updateEditPreview();
}
function updateEditPreview(){
  const ta=document.getElementById('editTA');
  const prev=document.getElementById('editPreview');
  if(!ta||!prev)return;
  const html=ta.value
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.+?)\*\*/g,'<strong>$1</strong>')
    .replace(/\n\n+/g,'</p><p>').replace(/\n/g,'<br>');
  prev.innerHTML='<p>'+html+'</p>';
}
function checkOverflow(){
  const card=document.getElementById('theCard');
  const warn=document.getElementById('overflowWarn');
  if(!card)return;
  const h=card.offsetHeight;
  if(h>525){
    card.classList.add('has-overflow');
    if(warn)warn.classList.add('active');
  }else{
    card.classList.remove('has-overflow');
    if(warn)warn.classList.remove('active');
  }
}
// Keyboard shortcuts inside textarea
document.addEventListener('keydown',function(e){
  if(e.target&&e.target.id==='editTA'){
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='b'){
      e.preventDefault();
      wrapBoldSelection();
    }
    // Ctrl+S OU Ctrl+Enter salva a edicao
    else if(((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='s')||
            ((e.ctrlKey||e.metaKey)&&e.key==='Enter')){
      e.preventDefault();
      saveEdit();
    }
    // Esc cancela edicao
    else if(e.key==='Escape'){
      e.preventDefault();
      editingText=false;
      render();
    }
  }
});
// Check overflow on render
const _origRender=typeof render==='function'?render:null;
if(_origRender){
  window.render=function(){_origRender.apply(this,arguments);setTimeout(checkOverflow,50);};
}

function getTextStyle(){
  return {
    family: document.getElementById('fontFamily').value,
    size: parseFloat(document.getElementById('fontSize').value)||18,
    lh: parseFloat(document.getElementById('lineHeight').value)||1.4,
    pg: parseInt(document.getElementById('paraGap').value)||20
  };
}
function applyTextStyle(){
  const st=getTextStyle();
  document.querySelectorAll('.text-display').forEach(el=>{
    el.style.fontFamily=st.family;
    el.style.fontSize=st.size+'px';
    el.style.lineHeight=st.lh;
    el.querySelectorAll(':scope > span').forEach(s=>{
      s.style.marginBottom=st.pg+'px';
    });
  });
  const edit=document.getElementById('editTA');
  if(edit){edit.style.fontFamily=st.family;edit.style.fontSize=st.size+'px';edit.style.lineHeight=st.lh;}
  try{localStorage.setItem(LS_KEY+'_style',JSON.stringify(st));}catch(e){}
}
function loadTextStyle(){
  try{
    const saved=JSON.parse(localStorage.getItem(LS_KEY+'_style')||'null');
    if(saved){
      document.getElementById('fontFamily').value=saved.family;
      document.getElementById('fontSize').value=saved.size;
      document.getElementById('lineHeight').value=saved.lh;
      document.getElementById('paraGap').value=saved.pg;
    }
  }catch(e){}
  applyTextStyle();
}
function resetTextStyle(){
  // Padrao Bearlz: Open Sans, 18px (=Canva 34pt), LH 1.4, gap 20px
  document.getElementById('fontFamily').value='Open Sans,sans-serif';
  document.getElementById('fontSize').value=18;
  document.getElementById('lineHeight').value=1.4;
  document.getElementById('paraGap').value=20;
  try{localStorage.removeItem(LS_KEY+'_style');}catch(e){}
  applyTextStyle();
}
let _previewCanvas=null;
async function showExportPreview(){
  setStatus('Gerando preview...');
  try{
    _previewCanvas=await captureCard();
    document.getElementById('previewImg').src=_previewCanvas.toDataURL('image/png');
    document.getElementById('previewModal').classList.add('active');
    setStatus('');
  }catch(e){setStatus('Erro no preview');console.error(e);}
}
function closePreview(){
  document.getElementById('previewModal').classList.remove('active');
}
function downloadFromPreview(){
  if(!_previewCanvas)return;
  const a=document.createElement('a');a.download='slide_'+(cur+1)+'.png';a.href=_previewCanvas.toDataURL('image/png');a.click();
}

async function captureCard(){
  const card=document.getElementById('theCard');
  // Card: 420x525 px (preview). Export em 2160x2700 (5.14x), que eh a
  // resolucao 2x do tamanho 1080x1350 do Instagram. Boa qualidade pra zoom.
  const CARD_W=420,CARD_H=525,SCALE=2160/CARD_W;

  // ── Elementos pra esconder (display:none — fora do fluxo) ──
  // Sao itens absolute ou que nao afetam o layout dos vizinhos.
  const displayNoneEls=Array.from(document.querySelectorAll(
    '.card-dots-row,.card-footer,#imgCtrlPanel,.img-overlay-btn,'+
    '.profile-edit-panel,.img-resize-handle,.img-placeholder,'+
    '#cardDeleteImgBtn'
  ));

  // ── Elementos pra esconder PRESERVANDO LAYOUT (visibility:hidden) ──
  // .gap-handle pode ter margin-top negativa (do drag) que afeta o
  // posicionamento da imagem. display:none faria essa margin sumir e
  // distorcer o export.
  const visHiddenEls=Array.from(document.querySelectorAll('.gap-handle'));

  const prevDisplay=displayNoneEls.map(el=>({
    val: el.style.getPropertyValue('display'),
    pri: el.style.getPropertyPriority('display')
  }));
  displayNoneEls.forEach(el=>{el.style.setProperty('display','none','important');});

  const prevVis=visHiddenEls.map(el=>el.style.visibility);
  visHiddenEls.forEach(el=>{el.style.visibility='hidden';});

  const origStyle={};
  ['borderRadius','boxShadow','border','width','maxWidth','height','minHeight','maxHeight','marginBottom','overflow'].forEach(k=>{origStyle[k]=card.style[k];});
  card.style.borderRadius='0';card.style.boxShadow='none';card.style.border='none';
  card.style.width=CARD_W+'px';card.style.maxWidth=CARD_W+'px';
  card.style.height=CARD_H+'px';card.style.minHeight=CARD_H+'px';card.style.maxHeight=CARD_H+'px';
  card.style.marginBottom='0';
  // Forca overflow:hidden pra clipar elementos com imgMarginTop muito
  // negativo ou imgH grande que extrapolam (sao o que aparece distorcido)
  card.style.overflow='hidden';
  // Remove temporariamente classe free-edit (grade 3x3 + tooltip) durante o export
  const imgCont=document.getElementById('imgContainer');
  const freeEditWasOn=imgCont&&imgCont.classList.contains('free-edit');
  if(freeEditWasOn)imgCont.classList.remove('free-edit');
  // Aguarda imagens dos slides carregarem (caso sejam URLs externas/Pexels)
  const imgs=Array.from(card.querySelectorAll('img'));
  await Promise.all(imgs.map(im=>{
    if(im.complete&&im.naturalWidth)return Promise.resolve();
    return new Promise(res=>{
      im.addEventListener('load',res,{once:true});
      im.addEventListener('error',res,{once:true});
      setTimeout(res,5000); // timeout de seguranca
    });
  }));
  await new Promise(r=>requestAnimationFrame(r));
  try{
    const canvas=await html2canvas(card,{
      scale:SCALE,width:CARD_W,height:CARD_H,
      useCORS:true,allowTaint:false,logging:false,
      backgroundColor:'#ffffff',
      imageTimeout:15000, // 15s pra CDNs lentas (Pexels, investing.com)
    });
    return canvas;
  }finally{
    displayNoneEls.forEach((el,i)=>{
      const p=prevDisplay[i];
      if(p.val){el.style.setProperty('display',p.val,p.pri||'');}
      else{el.style.removeProperty('display');}
    });
    visHiddenEls.forEach((el,i)=>{el.style.visibility=prevVis[i]||'';});
    Object.keys(origStyle).forEach(k=>{card.style[k]=origStyle[k];});
    if(freeEditWasOn&&imgCont)imgCont.classList.add('free-edit');
  }
}

async function downloadCurrent(){
  setStatus(`Gerando slide ${cur+1}...`);
  const s=slides[cur];
  if(s.image&&!s.image.startsWith('data:')){
    await new Promise(res=>{const img=new Image();img.crossOrigin='anonymous';img.onload=res;img.onerror=res;img.src=s.image;});
  }
  try{
    const canvas=await captureCard();
    const a=document.createElement('a');a.download=`slide_${cur+1}_${window.CAROUSEL_SLUG||'carousel'}.png`;a.href=canvas.toDataURL('image/png');a.click();
    setStatus('Slide baixado!');setTimeout(()=>setStatus(''),2500);
  }catch(e){setStatus('Erro ao gerar slide');console.error(e);}
}

async function downloadAll(){
  setStatus('Pre-carregando imagens...');
  await preloadImages();
  const orig=cur;
  for(let i=0;i<slides.length;i++){
    cur=i;render();
    setStatus(`Gerando ${i+1}/${slides.length}...`);
    await new Promise(r=>setTimeout(r,1000));
    try{
      const canvas=await captureCard();
      const a=document.createElement('a');a.download=`slide_${i+1}_${window.CAROUSEL_SLUG||'carousel'}.png`;a.href=canvas.toDataURL('image/png');a.click();
    }catch(e){console.error(e);}
    await new Promise(r=>setTimeout(r,300));
  }
  cur=orig;render();
  setStatus('Todos os slides baixados!');setTimeout(()=>setStatus(''),3000);
}

// autoLoad() removido daqui — agora é fallback dentro de initHydrate() no fim do arquivo

async function downloadZip(){
  if(typeof JSZip==='undefined'){setStatus('JSZip nao carregou. Verifique conexao.');return;}
  const zip=new JSZip();
  const slug=document.title.replace(/[^a-z0-9]/gi,'-').toLowerCase().slice(0,40)||'carrossel';
  const orig=cur;
  setStatus('Pre-carregando imagens...');
  await preloadImages();
  setStatus('Preparando ZIP...');
  for(let i=0;i<slides.length;i++){
    cur=i;render();
    setStatus(`Gerando ${i+1}/${slides.length} para ZIP...`);
    await new Promise(r=>setTimeout(r,1000));
    try{
      const canvas=await captureCard();
      const blob=await new Promise(res=>canvas.toBlob(res,'image/png'));
      zip.file(`slide_${i+1}.png`,blob);
    }catch(e){console.error('Slide',i+1,e);}
    await new Promise(r=>setTimeout(r,200));
  }
  // Slides com video: o canvas nao captura MP4, entao o arquivo entra
  // direto no ZIP (slide_N_video.mp4) pro usuario subir no Instagram.
  for(let i=0;i<slides.length;i++){
    if(!slides[i].video)continue;
    try{
      setStatus(`Baixando vídeo do slide ${i+1}...`);
      const vr=await fetch(slides[i].video);
      zip.file(`slide_${i+1}_video.mp4`,await vr.blob());
    }catch(e){console.error('video slide',i+1,e);}
  }
  cur=orig;render();
  setStatus('Criando ZIP...');
  const content=await zip.generateAsync({type:'blob',compression:'STORE'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(content);
  a.download=`${slug}.zip`;
  a.click();
  setStatus(`ZIP com ${slides.length} slides baixado!`);
  setTimeout(()=>setStatus(''),3500);
}

/* ── Feature: 3 hooks (Curiosidade / Dor / Promessa) para o slide 1 ── */

function _ensureHookButton(){
  if(document.getElementById('btnHooks'))return true;
  const footerActions=document.querySelector('.footer-actions');
  if(!footerActions)return false;
  const btn=document.createElement('button');
  btn.id='btnHooks';
  btn.className='footer-btn';
  btn.textContent='✨ Hooks';
  btn.title='Gera 3 variações do slide 1: Curiosidade / Dor / Promessa';
  btn.style.color='#a855f7'; // roxinho pra destacar
  btn.addEventListener('click',function(e){
    e.preventDefault();e.stopPropagation();
    _abrirHooksModal();
  });
  footerActions.appendChild(btn);
  return true;
}
// Tenta inserir o botao varias vezes ate conseguir (mais robusto que setTimeout unico).
function _retryHookButton(tries){
  if(_ensureHookButton())return;
  if(tries<=0)return;
  setTimeout(()=>_retryHookButton(tries-1),200);
}

/* ── Galeria das imagens extraidas dos links ──
   Permite usuario acessar manualmente as fotos que o sistema extraiu
   dos artigos colados no brief, mesmo que Claude nao tenha usado. */
function _ensureGalleryButton(){
  if(document.getElementById('btnGaleria'))return true;
  const footerActions=document.querySelector('.footer-actions');
  if(!footerActions)return false;
  const imgs=window.EXTRACTED_IMAGES||[];
  const btn=document.createElement('button');
  btn.id='btnGaleria';
  btn.className='footer-btn';
  // Mostra sempre — se tem links, mostra contador; senao, soh "Galeria"
  btn.textContent=imgs.length>0?`🖼 Galeria (${imgs.length})`:'🖼 Galeria';
  btn.title='Imagens extraídas dos links + busca no Pexels';
  btn.style.color='#059669';
  btn.addEventListener('click',function(e){
    e.preventDefault();e.stopPropagation();
    _abrirGaleriaModal();
  });
  footerActions.appendChild(btn);
  return true;
}
function _retryGalleryButton(tries){
  if(_ensureGalleryButton())return;
  if(tries<=0)return;
  setTimeout(()=>_retryGalleryButton(tries-1),200);
}

function _abrirGaleriaModal(){
  // Reusa o mesmo padrao do hooks: posta pro parent (que tem mais viewport)
  // ou renderiza local se standalone
  const imgs=window.EXTRACTED_IMAGES||[];
  if(window.parent && window.parent !== window){
    try{
      window.parent.postMessage({type:'bearlz-gallery-open',images:imgs},'*');
      return;
    }catch(e){console.warn('[Galeria] postMessage falhou',e);}
  }
  _galeriaModalLocal(imgs);
}
function _galeriaModalLocal(imgs){
  let m=document.getElementById('galeriaModal');
  if(!m){
    m=document.createElement('div');
    m.id='galeriaModal';
    m.className='hooks-modal'; // reusa estilo do hooks
    m.innerHTML=`
      <div class="hooks-inner" style="max-width:720px">
        <div class="hooks-head">
          <strong>Imagens dos links</strong>
          <button class="hooks-x" onclick="document.getElementById('galeriaModal').classList.remove('active')">×</button>
        </div>
        <div id="galeriaBody" class="hooks-body" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px"></div>
      </div>`;
    document.body.appendChild(m);
  }
  const body=document.getElementById('galeriaBody');
  body.innerHTML=imgs.map((im,i)=>`
    <div style="cursor:pointer;border:2px solid #e5e7eb;border-radius:8px;overflow:hidden;transition:border-color .15s" data-idx="${i}">
      <img src="${im.url_imagem}" loading="lazy" style="width:100%;height:120px;object-fit:cover;display:block">
    </div>
  `).join('');
  body.querySelectorAll('[data-idx]').forEach(d=>{
    d.addEventListener('mouseenter',()=>d.style.borderColor='#1d9bf0');
    d.addEventListener('mouseleave',()=>d.style.borderColor='#e5e7eb');
    d.addEventListener('click',()=>{
      const im=imgs[parseInt(d.dataset.idx,10)];
      _aplicarImagemDaGaleria(im.url_imagem);
      m.classList.remove('active');
    });
  });
  m.classList.add('active');
}

// Converte URL externa em data: base64 via canvas (evita problema de CORS
// no export). Retorna data URL ou null se falhar.
async function _urlParaBase64(url){
  return new Promise((res)=>{
    const im=new Image();
    im.crossOrigin='anonymous';
    im.onload=()=>{
      try{
        const c=document.createElement('canvas');
        c.width=im.naturalWidth;c.height=im.naturalHeight;
        c.getContext('2d').drawImage(im,0,0);
        res(c.toDataURL('image/jpeg',0.92));
      }catch(e){res(null);}
    };
    im.onerror=()=>res(null);
    im.src=url;
  });
}

async function _aplicarImagemDaGaleria(url){
  if(!url||!slides[cur])return;
  const s=slides[cur];
  s.zoom=1;s.ox=50;s.oy=50;
  s.imgH=null;s.fit='cover';s.imgNW=null;s.imgNH=null;
  s.freeX=null;s.freeY=null;
  if(typeof setStatus==='function')setStatus('Carregando imagem...');
  // Pra URLs externas (Pexels, sites), converte em data: base64 ANTES de aplicar.
  // Isso garante que o export PNG inclua a foto mesmo se o servidor original
  // nao mandar CORS direito.
  let finalUrl=url;
  if(!url.startsWith('data:')){
    // Tenta direto. Se falhar (canvas tainted), tenta via proxy do servidor.
    let dataUrl=await _urlParaBase64(url);
    if(!dataUrl){
      dataUrl=await _urlParaBase64('/api/img-proxy?url='+encodeURIComponent(url));
    }
    if(dataUrl){
      finalUrl=dataUrl;
    }else{
      if(typeof setStatus==='function')setStatus('⚠ Imagem aplicada mas pode falhar no export');
      setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},3500);
    }
  }
  s.image=finalUrl;
  if(typeof render==='function')render();
  if(typeof autoSave==='function')autoSave();
  if(typeof setStatus==='function')setStatus('✓ Imagem aplicada no slide '+(cur+1));
  setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},2500);
}
// Listener pra receber imagem escolhida do parent (quando renderiza la fora)
window.addEventListener('message',function(e){
  if(!e.data||typeof e.data!=='object')return;
  if(e.data.type==='bearlz-gallery-apply' && e.data.url){
    _aplicarImagemDaGaleria(e.data.url);
  }
});

function _ensureHooksModal(){
  if(document.getElementById('hooksModal'))return;
  const modal=document.createElement('div');
  modal.id='hooksModal';
  modal.className='hooks-modal';
  modal.innerHTML=`
    <div class="hooks-inner" id="hooksInner">
      <div class="hooks-head">
        <strong>3 variações do slide 1</strong>
        <button class="hooks-x" id="hooksClose" type="button">×</button>
      </div>
      <div id="hooksBody" class="hooks-body">
        <div class="hooks-loading">Gerando com Claude… (15–30s)</div>
      </div>
    </div>`;
  document.body.appendChild(modal);
  // Listeners ROBUSTOS: clique no backdrop fecha; clique dentro do inner nao
  // fecha (sem stopPropagation inline pra nao quebrar listeners delegados).
  modal.addEventListener('click',function(e){
    if(e.target===modal) _fecharHooksModal();
  });
  document.getElementById('hooksClose').addEventListener('click',function(e){
    e.preventDefault();e.stopPropagation();
    _fecharHooksModal();
  });
}

function _fecharHooksModal(){
  const m=document.getElementById('hooksModal');
  if(m)m.classList.remove('active');
}
window._fecharHooksModal=_fecharHooksModal;

async function _abrirHooksModal(){
  // Se estamos dentro de iframe (viewer page), pede pro parent renderizar.
  // Parent tem viewport de janela inteira, modal nao corta.
  if(window.parent && window.parent !== window){
    try{
      window.parent.postMessage({type:'bearlz-hooks-open',slug:window.CAROUSEL_SLUG},'*');
      return;
    }catch(e){
      console.warn('[Hooks] postMessage falhou, abrindo modal local',e);
    }
  }
  // Standalone (sem iframe): renderiza modal aqui mesmo
  _ensureHooksModal();
  const modal=document.getElementById('hooksModal');
  const body=document.getElementById('hooksBody');
  modal.classList.add('active');
  body.innerHTML='<div class="hooks-loading">Gerando com Claude… (15–30s)</div>';
  const slug=window.CAROUSEL_SLUG;
  if(!slug){
    body.innerHTML='<div class="hooks-err">Slug do carrossel não encontrado. Recarregue a página.</div>';
    return;
  }
  try{
    const r=await fetch(`/api/hooks/${encodeURIComponent(slug)}`,{
      method:'POST',headers:{'Content-Type':'application/json'},body:'{}'
    });
    const raw=await r.text();
    let d;
    try{d=JSON.parse(raw);}
    catch(parseErr){
      console.error('[Hooks] Falha parse JSON:',parseErr,'status:',r.status,'raw:',raw.slice(0,200));
      if(r.status===500||r.status===502||r.status===504){
        body.innerHTML='<div class="hooks-err">Servidor demorou demais. Tente novamente em 30s.</div>';
      }else{
        body.innerHTML=`<div class="hooks-err">Resposta inválida do servidor (status ${r.status}).</div>`;
      }
      return;
    }
    if(!d.ok){
      console.error('[Hooks] API retornou erro:',d);
      body.innerHTML=`<div class="hooks-err">${d.error||'Erro ao gerar hooks'}</div>`;
      return;
    }
    const corMap={Curiosidade:'curi',Dor:'dor',Promessa:'prom'};
    window._hooksVariantes=d.variantes;
    body.innerHTML=d.variantes.map((v,i)=>{
      const klass=corMap[v.tipo]||'curi';
      const esc=(v.texto||'').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const html=esc.replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>').replace(/\n/g,'<br>');
      return `<div class="hook-card" data-idx="${i}">
        <div class="hook-label hook-${klass}">${v.tipo}</div>
        <div class="hook-text">${html}</div>
        <button class="hook-apply" type="button" data-hook-idx="${i}">Usar este</button>
      </div>`;
    }).join('');
    // Listeners por click event (em vez de inline onclick — funciona melhor
    // dentro de iframe e nao depende de funcoes globais)
    body.querySelectorAll('.hook-apply').forEach(btn=>{
      btn.addEventListener('click',function(e){
        e.preventDefault();e.stopPropagation();
        const idx=parseInt(btn.dataset.hookIdx,10);
        _aplicarHook(idx);
      });
    });
  }catch(e){
    console.error('[Hooks] Erro de fetch:',e);
    body.innerHTML=`<div class="hooks-err">Erro de conexão: ${e.message||'desconhecido'}</div>`;
  }
}
window._abrirHooksModal=_abrirHooksModal;

function _aplicarHook(i){
  const v=(window._hooksVariantes||[])[i];
  if(!v||!slides[0]){
    console.warn('[Hooks] Variante invalida ou slide 0 ausente. i=',i,'v=',v);
    return;
  }
  _aplicarVariante(v);
  _fecharHooksModal();
}
window._aplicarHook=_aplicarHook;

// Aplica uma variante (vinda do modal local OU do parent via postMessage)
function _aplicarVariante(v){
  if(!v||!slides[0])return;
  slides[0].text=v.texto;
  if(typeof cur!=='undefined')cur=0;
  if(typeof editingText!=='undefined')editingText=false;
  const panel=document.getElementById('imgCtrlPanel');
  if(panel)panel.style.display='none';
  if(typeof render==='function')render();
  if(typeof autoSave==='function')autoSave();
  if(typeof checkOverflow==='function')checkOverflow();
  if(typeof setStatus==='function')setStatus(`✓ Hook "${v.tipo}" aplicado no slide 1`);
  setTimeout(()=>{if(typeof setStatus==='function')setStatus('');},3500);
}

// Listener: parent posta 'bearlz-hooks-apply' depois que usuario escolhe
// uma variante no modal renderizado fora do iframe
window.addEventListener('message',function(e){
  if(!e.data||typeof e.data!=='object')return;
  if(e.data.type==='bearlz-hooks-apply' && e.data.variante){
    _aplicarVariante(e.data.variante);
  }
});

// Hidratação: tenta carregar do servidor primeiro, fallback pra localStorage
// Depois chama render(), loadTextStyle(), checkOverflow() e inicia polling
initHydrate();
// Adiciona botão Hooks no footer apos o DOM estar pronto. Tenta varias vezes
// caso o footer ainda nao tenha sido renderizado pelo initHydrate.
setTimeout(()=>_retryHookButton(20),100);
setTimeout(()=>_retryGalleryButton(20),150);
// Bind do handle de gap entre texto e imagem (com retry caso DOM nao
// esteja pronto, ou template antigo precise injetar o handle)
function _retryGapHandle(tries){
  initGapHandle();
  if(document.getElementById('gapHandle'))return;
  if(tries<=0)return;
  setTimeout(()=>_retryGapHandle(tries-1),200);
}
setTimeout(()=>_retryGapHandle(20),150);
