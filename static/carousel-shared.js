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

/* ── Auto-save / Auto-load ── */
const LS_KEY=window.CAROUSEL_LS_KEY||'bearlz_carousel_v1';
let _saveTimer;
function autoSave(){
  clearTimeout(_saveTimer);
  _saveTimer=setTimeout(()=>{
    try{
      const state={
        slides:slides.map(s=>{
          const c={...s};
          // Don't persist SVG chart images (they're rebuilt from code)
          if(c.image&&c.image.startsWith('data:image/svg+xml'))c.image=null;
          return c;
        }),
        profile:{...profile},
        avatar:avatarDataUrl,
        v:1
      };
      localStorage.setItem(LS_KEY,JSON.stringify(state));
      flashSaved();
    }catch(e){
      // If quota exceeded try without uploaded images
      try{
        const state={slides:slides.map(s=>{const c={...s};if(c.image&&c.image.startsWith('data:'))c.image=null;return c;}),profile:{...profile},v:1};
        localStorage.setItem(LS_KEY,JSON.stringify(state));
        flashSaved();
      }catch(e2){}
    }
  },400);
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
    if(state.v!==1)return;
    if(state.slides){
      state.slides.forEach((saved,i)=>{
        if(i>=slides.length)return;
        if(saved.text!=null)slides[i].text=saved.text;
        if(saved.zoom!=null)slides[i].zoom=saved.zoom;
        if(saved.ox!=null)slides[i].ox=saved.ox;
        if(saved.oy!=null)slides[i].oy=saved.oy;
        slides[i].imgH=saved.imgH??null;
        if(saved.fit)slides[i].fit=saved.fit;
        // Restore image only for non-SVG (Pexels URLs or uploaded photos saved ok)
        if(saved.image&&!saved.image.startsWith('data:image/svg'))slides[i].image=saved.image;
      });
    }
    if(state.profile){profile.name=state.profile.name;profile.handle=state.profile.handle;}
    if(state.avatar)avatarDataUrl=state.avatar;
  }catch(e){}
}
function clearSaved(){if(confirm('Apagar dados salvos e voltar ao original?')){localStorage.removeItem(LS_KEY);location.reload();}}

/* ── Quick image controls ── */
function setPos(ox,oy){
  slides[cur].ox=ox;slides[cur].oy=oy;
  const cont=document.getElementById('imgContainer');
  if(cont)cont.style.backgroundPosition=`${ox}% ${oy}%`;
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
    if(mode==='contain'){cont.style.backgroundSize='contain';cont.style.backgroundPosition='center';cont.style.backgroundColor='#ffffff';}
    else{cont.style.backgroundColor='';applyBgSize(slides[cur]);cont.style.backgroundPosition=`${slides[cur].ox||50}% ${slides[cur].oy||50}%`;}
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
  td.innerHTML=slides.map((_,i)=>`<button class="top-dot${i===cur?' active':''}" style="width:${i===cur?20:7}px" onclick="goTo(${i})"></button>`).join('');
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
  if(editingText){
    document.getElementById('textDisplay').style.display='none';
    document.getElementById('textEditArea').style.display='block';
    document.getElementById('editTA').value=s.text;
  }else{
    document.getElementById('textDisplay').style.display='block';
    document.getElementById('textEditArea').style.display='none';
    const paras=s.text.split(/\n\n+/).map(p=>`<span style="display:block;margin-bottom:16px">${toHTML(p)}</span>`).join('');
    document.getElementById('textDisplay').innerHTML=paras;
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
  return s.fit||'cover';
}

function renderImgSection(){
  const s=slides[cur];
  const sec=document.getElementById('imgSection');
  const panel=document.getElementById('imgCtrlPanel');
  if(s.image){
    const fit=getEffectiveFit(s);
    const hStyle=s.imgH?`height:${s.imgH}px;flex:none`:'';
    const bgStyle=fit==='contain'
      ?`background-image:url('${s.image}');background-size:contain;background-position:center;background-repeat:no-repeat;background-color:#f8f8f8;${hStyle}`
      :`background-image:url('${s.image}');background-size:cover;background-position:${s.ox||50}% ${s.oy||50}%;${hStyle}`;
    sec.innerHTML=`<div class="img-section">
      <div class="img-container" id="imgContainer" style="${bgStyle}">
        <button class="img-overlay-btn" style="top:8px;right:8px" onclick="clearImage()">×</button>
      </div>
    </div>`;
    document.getElementById('btnAjustar').style.display='inline';
    if(fit==='cover')initDrag();
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
    const hvals=[120,200,300,null];
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

/* ── Drag to pan ── */
function initDrag(){
  const cont=document.getElementById('imgContainer');
  if(!cont)return;
  let dragging=false,startX=0,startY=0,startOx=0,startOy=0;
  const s=slides[cur];
  const onDown=(e)=>{
    e.preventDefault();dragging=true;
    const pt=e.touches?e.touches[0]:e;
    startX=pt.clientX;startY=pt.clientY;startOx=s.ox||50;startOy=s.oy||50;
    window.addEventListener('mousemove',onMove);window.addEventListener('mouseup',onUp);
    window.addEventListener('touchmove',onMove,{passive:false});window.addEventListener('touchend',onUp);
  };
  const onMove=(e)=>{
    if(!dragging)return;e.preventDefault();
    const pt=e.touches?e.touches[0]:e;
    const r=cont.getBoundingClientRect();
    const dx=((pt.clientX-startX)/r.width)*100;
    const dy=((pt.clientY-startY)/r.height)*100;
    slides[cur].ox=Math.max(0,Math.min(100,Math.round(startOx-dx)));
    slides[cur].oy=Math.max(0,Math.min(100,Math.round(startOy-dy)));
    cont.style.backgroundPosition=`${slides[cur].ox}% ${slides[cur].oy}%`;
  };
  const onUp=()=>{
    dragging=false;
    window.removeEventListener('mousemove',onMove);window.removeEventListener('mouseup',onUp);
    window.removeEventListener('touchmove',onMove);window.removeEventListener('touchend',onUp);
    updateImgCtrlUI(slides[cur]);autoSave();
  };
  cont.addEventListener('mousedown',onDown);
  cont.addEventListener('touchstart',onDown,{passive:false});
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
function startEdit(){editingText=true;render();setTimeout(()=>document.getElementById('editTA').focus(),10);}
function liveUpdate(val){slides[cur].text=val;const paras=val.split(/\n\n+/).map(p=>`<span style="display:block;margin-bottom:16px">${toHTML(p)}</span>`).join('');document.getElementById('textDisplay').innerHTML=paras;}
function saveEdit(){slides[cur].text=document.getElementById('editTA').value;editingText=false;render();autoSave();}

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
function clearImage(){slides[cur].image=null;slides[cur].zoom=1;slides[cur].ox=50;slides[cur].oy=50;slides[cur].imgH=null;slides[cur].fit=null;slides[cur].imgNW=null;slides[cur].imgNH=null;document.getElementById('imgCtrlPanel').style.display='none';render();autoSave();}
function toggleImgCtrl(){const panel=document.getElementById('imgCtrlPanel');const showing=panel.style.display!=='none';panel.style.display=showing?'none':'block';}
function applyBgSize(s){
  const cont=document.getElementById('imgContainer');if(!cont)return;
  const z=s.zoom||1;
  if(!s.imgNW||!s.imgNH){
    if(z>1){const t=new Image();t.crossOrigin='anonymous';t.onload=()=>{s.imgNW=t.naturalWidth;s.imgNH=t.naturalHeight;applyBgSize(s);};t.src=s.image;}
    cont.style.backgroundSize='cover';return;
  }
  const cw=cont.offsetWidth,ch=cont.offsetHeight;if(!cw||!ch)return;
  const base=Math.max(cw/s.imgNW,ch/s.imgNH);
  cont.style.backgroundSize=`${Math.round(s.imgNW*base*z)}px ${Math.round(s.imgNH*base*z)}px`;
}
function updateZoom(val){slides[cur].zoom=parseFloat(val);document.getElementById('valZoom').textContent=Math.round(val*100)+'%';applyBgSize(slides[cur]);updateImgCtrlUI(slides[cur]);autoSave();}
function updateOx(val){slides[cur].ox=parseInt(val);document.getElementById('valOx').textContent=val+'%';const cont=document.getElementById('imgContainer');if(cont)cont.style.backgroundPosition=`${slides[cur].ox}% ${slides[cur].oy}%`;updateImgCtrlUI(slides[cur]);autoSave();}
function updateOy(val){slides[cur].oy=parseInt(val);document.getElementById('valOy').textContent=val+'%';const cont=document.getElementById('imgContainer');if(cont)cont.style.backgroundPosition=`${slides[cur].ox}% ${slides[cur].oy}%`;updateImgCtrlUI(slides[cur]);autoSave();}
function updateImgH(val){slides[cur].imgH=parseInt(val);document.getElementById('valH').textContent=val+'px';const cont=document.getElementById('imgContainer');if(cont){cont.style.height=val+'px';cont.style.flex='none';}updateImgCtrlUI(slides[cur]);autoSave();}
function resetImgCtrl(){
  slides[cur].zoom=1;slides[cur].ox=50;slides[cur].oy=50;slides[cur].imgH=null;slides[cur].fit=null;
  renderImgSection();
  autoSave();
}

/* ── Add / Remove slides ── */
function addSlide(){
  const nid=Math.max(...slides.map(x=>x.id))+1;
  slides.splice(cur+1,0,{id:nid,text:`Novo slide. Use **negrito** para destacar.`,image:null,zoom:1,ox:50,oy:50});
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
    const segs=parseBold(paras[pi].replace(/\n/g,' '));
    const lines=canvasWrapText(ctx,segs,maxW,fSize);
    for(const ln of lines){
      let lx=px;
      for(const chunk of ln){
        ctx.font=`${chunk.bold?'700':'400'} ${fSize}px Open Sans,sans-serif`;
        ctx.fillStyle='#0f1419';
        ctx.fillText(chunk.text,lx,cy);
        lx+=ctx.measureText(chunk.text).width;
      }
      cy+=lh;
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
      ctx.save();roundRect(ctx,px,cy,sw,sh,radius);ctx.clip();
      if(fitMode==='contain'){
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
    size: parseFloat(document.getElementById('fontSize').value)||18.5,
    lh: parseFloat(document.getElementById('lineHeight').value)||1.5,
    pg: parseInt(document.getElementById('paraGap').value)||16
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
  document.getElementById('fontFamily').value='Open Sans,sans-serif';
  document.getElementById('fontSize').value=18.5;
  document.getElementById('lineHeight').value=1.5;
  document.getElementById('paraGap').value=16;
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
  const CARD_W=420,CARD_H=525,SCALE=2160/CARD_W;
  // Measure heights of UI elements BEFORE hiding (so we can replace with spacers)
  const dotsEl=document.querySelector('.card-dots-row');
  const footerEl=document.querySelector('.card-footer');
  const dotsH=dotsEl?dotsEl.offsetHeight:0;
  const footerH=footerEl?footerEl.offsetHeight:0;
  const hideEls=Array.from(document.querySelectorAll('.card-dots-row,.card-footer,#imgCtrlPanel,.img-overlay-btn,.profile-edit-panel'));
  const prevDisplay=hideEls.map(el=>el.style.display);
  hideEls.forEach(el=>{el.style.display='none';});
  // Add spacer inside card to preserve image container size (matches preview exactly)
  const spacer=document.createElement('div');
  spacer.style.height=(dotsH+footerH)+'px';
  spacer.style.flexShrink='0';
  spacer.style.width='100%';
  spacer.setAttribute('data-capture-spacer','1');
  card.appendChild(spacer);
  const origStyle={};
  ['borderRadius','boxShadow','border','width','maxWidth','height','minHeight','maxHeight'].forEach(k=>{origStyle[k]=card.style[k];});
  card.style.borderRadius='0';card.style.boxShadow='none';card.style.border='none';
  card.style.width=CARD_W+'px';card.style.maxWidth=CARD_W+'px';
  card.style.height=CARD_H+'px';card.style.minHeight=CARD_H+'px';card.style.maxHeight=CARD_H+'px';
  await new Promise(r=>requestAnimationFrame(r));
  try{
    const canvas=await html2canvas(card,{
      scale:SCALE,width:CARD_W,height:CARD_H,
      useCORS:true,allowTaint:false,logging:false,backgroundColor:'#ffffff'
    });
    return canvas;
  }finally{
    hideEls.forEach((el,i)=>{el.style.display=prevDisplay[i];});
    Object.keys(origStyle).forEach(k=>{card.style[k]=origStyle[k];});
    if(spacer.parentNode)spacer.parentNode.removeChild(spacer);
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

autoLoad();

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

render();
loadTextStyle();
checkOverflow();
