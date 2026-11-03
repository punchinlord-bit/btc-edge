(function(){
// Two independent charts, 15-minute rounds. Default view is a LIVE STREAM (last ~150s, rocket pinned at 78%,
// riding the line against the target). Tap the chart to cycle 60s -> 150s -> full 15m window.
//  polymarket: Chainlink BTC/USD. Target = price at open. Up if close >= target.
//  kalshi:     CF BRTI. Target = 60s average before open. Settles on 60s average before close.
const WIN=900,G='#00f59b',R='#ff3b69',C='#38bdf8',AM='#f59e0b';
const qs=new URLSearchParams(location.search),DEBUG=qs.get('debug')==='1';
const API=(qs.get('api')||'')+'/api/btc-15m';
const VIEWS=[90,300,900];let VIEW=+qs.get('view')||90;
let clockOff=0,demoOn=false,realSeen=false;
const nowS=()=>(Date.now()+clockOff)/1000;
const fmt=v=>'$'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});
const VENUES={polymarket:{name:'POLYMARKET',src:'CHAINLINK BTC/USD',avg:false},kalshi:{name:'KALSHI',src:'CF BRTI 60s AVG',avg:true}};
const charts={};
function randn(){return Math.sqrt(-2*Math.log(Math.random()||1e-9))*Math.cos(6.2831853*Math.random())}
// accepts unix seconds, unix ms, numeric strings, ISO strings
function normTs(t){if(t==null)return null;if(typeof t==='string')t=/^\d+(\.\d+)?$/.test(t)?+t:Date.parse(t);
  if(typeof t!=='number'||!isFinite(t))return null;if(t>1e14)t/=1e6;else if(t>1e11)t/=1000;return t}

class Chart{
  constructor(k,host){
    this.k=k;this.v=VENUES[k];this.view=VIEW;this.ov={};this.pOver=null;this.ylo=null;this.yhi=null;this.off=0;
    this.bets={};this.tally={w:0,l:0,pnl:0};this.ang=0;this.sh={};
    this.stars=Array.from({length:46},()=>({x:Math.random(),y:Math.random(),s:Math.random()*1.2+.3,p:Math.random()*6}));
    this.cv=document.createElement('canvas');this.cv.style.cssText='position:absolute;inset:0;width:100%;height:100%;display:block';if(getComputedStyle(host).position==='static')host.style.position='relative';host.appendChild(this.cv);this.ctx=this.cv.getContext('2d');
    this.W=0;this.H=0;new ResizeObserver(()=>this.size()).observe(host);this.size();this.reset();
  }
  size(){const d=Math.min(devicePixelRatio||1,3),r=this.cv.getBoundingClientRect();this.W=r.width;this.H=r.height;this.cv.width=r.width*d;this.cv.height=r.height*d;this.ctx.setTransform(d,0,0,d,0,0)}
  setView(v){this.view=+v;this.ylo=null}
  reset(){this.sec=[];this.raw=null;this.dispM=null;this.dispRaw=null;this.prevM=null;this.ws=Math.floor(nowS()/WIN)*WIN;
    this.strike=null;this.approx=false;this.pts=[];this.rawPts=[];this.vel=0;this.int=0;this.shake=0;
    this.parts=[];this.rings=[];this.res=null;this.last=0;this.side=null;this.bad=0;this.rej=0;this.rp=[];this.pend=null;this.nt=0;this.lastT=0;this.skewed=false;this.sh={}}
  avgRange(a,b){let s=0,n=0;for(let i=this.sec.length-1;i>=0;i--){const x=this.sec[i];if(x.s<a)break;if(x.s<=b){s+=x.p;n++}}return{m:n?s/n:null,n}}
  metricNow(now){if(!this.v.avg)return this.raw;const f=Math.floor(now),a=this.avgRange(f-59,f);return a.n?a.m:this.raw}
  tick(p,ts,demo){
    p=Number(p);if(!isFinite(p)||p<=0)return;
    if(!demo&&!realSeen){realSeen=true;if(demoOn){demoOn=false;Object.values(charts).forEach(c=>{c.reset();c.bets={};c.tally={w:0,l:0,pnl:0}})}}
    // price sanity: BTC does not move 2% in one tick. Stops bet prices / cents / garbage from hijacking the line.
    const rpS=this.rp.slice().sort((a,b)=>a-b),rm=rpS.length>=3?rpS[rpS.length>>1]:null;
    if(rm!=null&&Math.abs(p/rm-1)>.0005){
      if(this.pend!=null&&Math.abs(p/this.pend-1)<.0003){this.pend=null;this.rp=[p,p,p]}   // confirmed level shift
      else{this.pend=p;this.rej++;return}                                                   // lone spike: drop
    }else this.pend=null;
    this.bad=0;
    const n=nowS();let t=normTs(ts);
    if(t==null||Math.abs(t-n)>120){t=n;this.skewed=ts!=null}else this.skewed=false;
    t+=this.off;
    const s=Math.floor(t),L=this.sec[this.sec.length-1];
    if(L&&L.s===s)L.p=p;else if(!L||s>L.s)this.sec.push({s,p});
    if(this.sec.length>1300)this.sec.shift();
    if(this.raw!=null&&Math.abs(p-this.raw)>12)this.shake=Math.min(1,Math.abs(p-this.raw)/40);
    this.raw=p;this.rp.push(p);if(this.rp.length>5)this.rp.shift();this.nt++;this.lastT=n;
    if(this.dispM==null){this.dispM=this.metricNow(t);this.dispRaw=p}
  }
  setStrike(ws,p){this.ov[ws]=p;if(ws===this.ws){this.strike=p;this.approx=false;this.sh[ws]=p}}
  addBet(b){const ws=Math.floor(b.ts/WIN)*WIN;if(ws<this.ws-WIN)return;(this.bets[ws]=this.bets[ws]||{})[b.id]=b}
  betPnl(b,win){const q=b.qty||0,c=(b.cents==null?50:b.cents)/100;return win?q*(1-c):-q*c}
  seed(arr){arr.forEach(x=>this.tick(x.p,x.t,true));this.pts=[];this.rawPts=[];
    this.sec.forEach(x=>{if(x.s>=this.ws-1800)this.addPt(x.s,this.v.avg?this.avgRange(x.s-59,x.s).m:x.p,x.p,Math.floor(x.s/WIN)*WIN)})}
  addPt(t,v,r,w){this.pts.push({t,v,w});if(this.v.avg)this.rawPts.push({t,v:r,w});
    const cut=t-1900;while(this.pts.length&&this.pts[0].t<cut)this.pts.shift();while(this.rawPts.length&&this.rawPts[0].t<cut)this.rawPts.shift()}
  derive(){
    if(this.ov[this.ws]!=null){this.strike=this.ov[this.ws];this.approx=false}
    else if(this.v.avg){const a=this.avgRange(this.ws-60,this.ws-1);
      if(a.n>=20){this.strike=a.m;this.approx=false}else{const b=this.avgRange(this.ws,this.ws+59);this.strike=b.n?b.m:this.raw;this.approx=true}}
    else{const f=this.sec.find(x=>x.s>=this.ws);this.strike=f?f.p:this.raw;this.approx=!(f&&f.s<=this.ws+2)}
    this.sh[this.ws]=this.strike;
  }
  rollover(now){
    const ws=Math.floor(now/WIN)*WIN;if(ws===this.ws)return;
    if(this.strike!=null&&this.raw!=null){
      let e;if(this.v.avg)e=this.avgRange(this.ws+WIN-60,this.ws+WIN-1).m;
      else{for(let i=this.sec.length-1;i>=0;i--)if(this.sec[i].s<=ws){e=this.sec[i].p;break}}
      if(e==null)e=this.dispM;
      const up=e>=this.strike,bs=Object.values(this.bets[this.ws]||{}).filter(b=>b.status!=='cancelled');let pnl=0,wn=0;
      bs.forEach(b=>{const win=(b.side==='UP')===up;pnl+=this.betPnl(b,win);win?wn++:this.tally.l++});
      this.tally.w+=wn;this.tally.pnl+=pnl;this.res={up,until:now+5,n:bs.length,pnl};this.shake=1;
    }
    delete this.bets[this.ws];this.ws=ws;this.strike=null;this.side=null;
    for(const k of Object.keys(this.sh))if(+k<ws-2*WIN)delete this.sh[k];
    if(this.raw!=null)this.derive();
  }
  step(dt){
    const now=nowS();this.rollover(now);
    if(this.raw!=null){
      if(this.strike==null)this.derive();
      const m=this.metricNow(now);
      this.dispM+=(m-this.dispM)*(1-Math.exp(-dt*(this.v.avg?4:11)));
      this.dispRaw+=(this.raw-this.dispRaw)*(1-Math.exp(-dt*11));
      const v=(this.dispM-(this.prevM==null?this.dispM:this.prevM))/Math.max(dt,.001);this.prevM=this.dispM;
      this.vel+=(v-this.vel)*Math.min(1,dt*3);
      this.int+=(Math.min(1,Math.abs(this.vel)/(this.v.avg?8:22))-this.int)*Math.min(1,dt*8);
      if(now-this.last>.5){this.last=now;this.addPt(now,this.dispM,this.dispRaw,this.ws)}
      const up=this.dispM>=this.strike;
      if(this.side!=null&&up!==this.side){this.rings.push({r:6,l:1,c:up?G:R});this.shake=Math.max(this.shake,.6)}
      this.side=up;
    }
    this.draw(now);
  }
  draw(now){
    const x=this.ctx,W=this.W,H=this.H;x.clearRect(0,0,W,H);
    const L=8,Rr=W-54,T=30,B=H-10,pw=Rr-L,rem=Math.max(0,this.ws+WIN-now),sig=1.4,stream=this.view<WIN;
    x.save();
    if(this.shake>.02){x.translate((Math.random()-.5)*this.shake*6,(Math.random()-.5)*this.shake*6);this.shake*=.86}else this.shake=0;
    const mm=Math.ceil(rem),clk=String(Math.floor(mm/60)).padStart(2,'0')+':'+String(mm%60).padStart(2,'0');
    x.font='800 11px -apple-system,sans-serif';x.textAlign='left';x.fillStyle='#7f8da3';
    x.fillText(this.v.name+' · '+this.v.src+(demoOn?' · DEMO':'')+(this.approx?' · TARGET EST':''),10,18);
    x.textAlign='right';x.font='900 15px -apple-system,sans-serif';x.fillStyle=rem<30?R:rem<120?AM:'#cbd5e1';x.fillText(clk,W-10,19);
    if(this.raw==null||this.strike==null){x.textAlign='center';x.fillStyle='#5d6b82';x.font='700 12px -apple-system,sans-serif';x.fillText('waiting for '+this.v.src+' ticks',W/2,H/2);x.restore();return}

    // x mapping: live stream (scrolling, head pinned) or full 15m window
    const hxFix=L+pw*.78,pps=(hxFix-L)/this.view;
    const xOf=stream?(t=>hxFix-(now-t)*pps):(t=>L+(t-this.ws)/WIN*pw);
    const tmin=stream?now-this.view-1:this.ws,hx=xOf(now);
    // y range from VISIBLE points + target
    const fd=this.vel*Math.min(rem,20)*.35,fm=this.dispM*.0006,fair=this.dispM+Math.max(-fm,Math.min(fm,fd));
    let lo=this.dispM,hi=this.dispM;
    for(let i=this.pts.length-1;i>=0;i--){const p=this.pts[i];if(p.t<tmin)break;if(!stream&&p.w!==this.ws)continue;if(p.v<lo)lo=p.v;if(p.v>hi)hi=p.v}
    for(let i=this.rawPts.length-1;i>=0;i--){const p=this.rawPts[i];if(p.t<tmin)break;if(p.v<lo)lo=p.v;if(p.v>hi)hi=p.v}
    const minSp=this.dispM*.00025;let sp0=Math.max(hi-lo,minSp);
    if(this.strike>=lo-sp0*1.5&&this.strike<=hi+sp0*1.5){lo=Math.min(lo,this.strike);hi=Math.max(hi,this.strike)}
    sp0=Math.max(hi-lo,minSp);lo-=sp0*.22;hi+=sp0*.22;
    if(this.ylo==null){this.ylo=lo;this.yhi=hi}else{this.ylo+=(lo-this.ylo)*.1;this.yhi+=(hi-this.yhi)*.1}
    const ylo=this.ylo,yhi=this.yhi,sc=(B-T)/(yhi-ylo),span=yhi-ylo;
    const yOf=v=>T+(1-(v-ylo)/(yhi-ylo))*(B-T);
    const hy=yOf(this.dispM),sy=yOf(this.strike),up=this.dispM>=this.strike,col=up?G:R;

    const bg=x.createRadialGradient(hx,hy,0,hx,hy,W*.6);bg.addColorStop(0,col+'1c');bg.addColorStop(1,'transparent');
    x.fillStyle=bg;x.fillRect(0,0,W,H);
    this.stars.forEach(s=>{const sx=((s.x*W-now*s.s*1.5)%W+W)%W;x.globalAlpha=.25+.25*Math.sin(now*2+s.p);x.fillStyle='#9fb4d6';x.fillRect(sx,s.y*H,s.s,s.s)});x.globalAlpha=1;
    x.font='700 10.5px -apple-system,sans-serif';x.textAlign='right';
    for(let i=0;i<5;i++){const v=ylo+span*i/4,y=yOf(v);x.strokeStyle='#ffffff08';x.beginPath();x.moveTo(L,y);x.lineTo(Rr,y);x.stroke();
      x.fillStyle='#5d6b82';x.fillText('$'+v.toLocaleString('en-US',{minimumFractionDigits:span<30?1:0,maximumFractionDigits:span<30?1:0}),W-6,y-3)}
    // round boundaries + Kalshi averaging zone
    const vline=(t,txt)=>{const px=xOf(t);if(px<L||px>Rr)return;x.setLineDash([2,5]);x.strokeStyle='#ffffff30';x.lineWidth=1;x.beginPath();x.moveTo(px,T);x.lineTo(px,B);x.stroke();x.setLineDash([]);
      x.textAlign='left';x.fillStyle='#8a97ab';x.font='800 9px -apple-system,sans-serif';x.fillText(txt,px+3,T+10)};
    if(this.v.avg){const a=Math.max(L,xOf(this.ws+WIN-60)),b=Math.min(Rr,xOf(this.ws+WIN));if(b>a){x.fillStyle='rgba(245,158,11,.07)';x.fillRect(a,T,b-a,B-T);x.textAlign='left';x.fillStyle='#f59e0b99';x.font='800 9px -apple-system,sans-serif';x.fillText('AVG ZONE',a+4,B-4)}}
    if(stream){vline(this.ws,'OPEN');vline(this.ws+WIN,'SETTLE')}
    // previous round's target (faint) so the break at rollover is readable
    const os=this.sh[this.ws-WIN];
    if(stream&&os!=null){const y=yOf(os),a=Math.max(L,xOf(this.ws-WIN)),b=Math.min(Rr,xOf(this.ws));if(b>a&&y>T&&y<B){x.setLineDash([4,5]);x.strokeStyle='#94a3b855';x.beginPath();x.moveTo(a,y);x.lineTo(b,y);x.stroke();x.setLineDash([])}}
    // faint raw BRTI
    if(this.v.avg){const rp=this.rawPts.filter(p=>p.t>=tmin&&(stream||p.w===this.ws));
      if(rp.length){x.beginPath();rp.forEach((p,i)=>{const px=xOf(p.t),py=yOf(p.v);i?x.lineTo(px,py):x.moveTo(px,py)});x.lineTo(hx,yOf(this.dispRaw));x.strokeStyle='rgba(255,255,255,.2)';x.lineWidth=1;x.stroke()}}
    // previous-round part of the line (neutral), then current round colored vs target
    const old=this.pts.filter(p=>p.t>=tmin&&p.w<this.ws),cur=this.pts.filter(p=>p.t>=tmin&&p.w===this.ws);
    if(stream&&old.length){x.beginPath();old.forEach((p,i)=>{const px=xOf(p.t),py=yOf(p.v);i?x.lineTo(px,py):x.moveTo(px,py)});
      const f=cur[0];if(f)x.lineTo(xOf(f.t),yOf(f.v));x.strokeStyle='rgba(180,195,215,.45)';x.lineWidth=2;x.lineJoin='round';x.stroke()}
    const pts=cur.map(p=>[xOf(p.t),yOf(p.v)]);
    if(stream&&old.length&&!cur.length)pts.push([xOf(old[old.length-1].t),yOf(old[old.length-1].v)]);
    pts.push([hx,hy]);if(pts.length<2)pts.unshift([Math.max(L,hx-6),hy]);
    const trace=()=>{x.beginPath();x.moveTo(pts[0][0],pts[0][1]);for(let i=1;i<pts.length-1;i++){const mx=(pts[i][0]+pts[i+1][0])/2,my=(pts[i][1]+pts[i+1][1])/2;x.quadraticCurveTo(pts[i][0],pts[i][1],mx,my)}const l=pts[pts.length-1];x.lineTo(l[0],l[1])};
    trace();x.lineTo(hx,B);x.lineTo(pts[0][0],B);x.closePath();
    const ag=x.createLinearGradient(0,Math.max(T,hy-40),0,B);ag.addColorStop(0,col+'40');ag.addColorStop(1,col+'00');x.fillStyle=ag;x.fill();
    [[0,sy,G],[sy,H,R]].forEach(([a,b,c])=>{x.save();x.beginPath();x.rect(0,a,W,Math.max(0,b-a));x.clip();
      x.shadowColor=c;x.shadowBlur=8+this.int*22;trace();x.strokeStyle=c;x.lineWidth=2.5+this.int*2;x.lineJoin='round';x.stroke();x.restore()});
    // current target line
    x.setLineDash([5,6]);x.strokeStyle='#94a3b8cc';x.lineWidth=1;x.beginPath();x.moveTo(L,sy);x.lineTo(Rr,sy);x.stroke();x.setLineDash([]);
    if(sy>=T&&sy<=B){x.textAlign='left';x.fillStyle='#94a3b8';x.font='800 10px -apple-system,sans-serif';x.fillText('TARGET '+fmt(this.strike),L+2,sy-5)}
    else{const above=sy<T,ty=above?T+6:B-22,dd=this.strike-this.dispM,tx=(above?'▲ ':'▼ ')+'TARGET '+fmt(this.strike)+' ('+(dd>=0?'+':'−')+'$'+Math.abs(dd).toFixed(0)+')';
      x.font='800 10px -apple-system,sans-serif';const tw=x.measureText(tx).width+14;x.fillStyle='#050810d9';x.beginPath();x.roundRect(Rr-tw-4,ty,tw,16,6);x.fill();x.textAlign='left';x.fillStyle='#94a3b8';x.fillText(tx,Rr-tw+3,ty+11)}
    // model cone
    const pc=this.pOver!=null?this.pOver:1/(1+Math.exp(-1.7*(fair-this.strike)/(sig*Math.sqrt(Math.max(1,rem)))));
    const horizon=stream?Math.min(rem,(Rr-hx)/pps):rem,ex=stream?hx+horizon*pps:Rr;
    if(rem>1&&ex-hx>6){const ey=yOf(fair),sp=sig*Math.sqrt(Math.min(horizon,120))*sc;
      x.beginPath();x.moveTo(hx,hy);x.lineTo(ex,ey-sp);x.lineTo(ex,ey+sp);x.closePath();
      const cg=x.createLinearGradient(hx,0,ex,0);cg.addColorStop(0,C+'44');cg.addColorStop(1,C+'08');x.fillStyle=cg;x.fill();
      x.setLineDash([4,4]);x.strokeStyle=C;x.lineWidth=1.4;x.beginPath();x.moveTo(hx,hy);x.lineTo(ex,ey);x.stroke();x.setLineDash([])}
    // bot bets for THIS venue and THIS round: small markers on the line + one average-entry line per side
    const bs=Object.values(this.bets[this.ws]||{}).filter(b=>b.status!=='cancelled').sort((a,b)=>a.ts-b.ts);
    const vAt=t=>{for(let i=this.pts.length-1;i>=0;i--)if(this.pts[i].t<=t)return this.pts[i].v;return this.dispM};
    let qU=0,qD=0,pn=0,sU=0,sD=0,x0U=1e9,x0D=1e9;const upNow=this.dispM>=this.strike;
    bs.forEach(b=>{const lv=vAt(b.ts),win=(b.side==='UP')===upNow;pn+=this.betPnl(b,win);
      if(b.side==='UP'){qU+=b.qty;sU+=lv*b.qty;x0U=Math.min(x0U,b.ts)}else{qD+=b.qty;sD+=lv*b.qty;x0D=Math.min(x0D,b.ts)}});
    [['UP',qU,sU,x0U,G],['DOWN',qD,sD,x0D,R]].forEach(([sd,q,s,t0,c])=>{if(!q)return;
      const y=yOf(s/q),a=Math.max(L,Math.min(Rr,xOf(t0)));if(y<T||y>B)return;
      x.setLineDash([2,3]);x.strokeStyle=c+'aa';x.lineWidth=1.3;x.beginPath();x.moveTo(a,y);x.lineTo(Math.max(a,hx),y);x.stroke();x.setLineDash([]);
      x.font='800 9px -apple-system,sans-serif';x.textAlign='left';x.fillStyle=c;x.fillText((sd==='UP'?'▲ ':'▼ ')+'ENTRY '+fmt(s/q).slice(0,-3),a+3,y-4)});
    bs.slice(-40).forEach(b=>{const bx=xOf(b.ts);if(bx<L||bx>Rr)return;const by=yOf(vAt(b.ts)),win=(b.side==='UP')===upNow,c=b.side==='UP'?G:R,s=Math.min(8,4+Math.sqrt(b.qty||1)*.6),dir=b.side==='UP'?-1:1;
      x.fillStyle=c;x.strokeStyle=win?'#fff':'#ffffff55';x.lineWidth=1.2;x.beginPath();x.moveTo(bx,by+dir*s);x.lineTo(bx-s*.8,by-dir*s*.6);x.lineTo(bx+s*.8,by-dir*s*.6);x.closePath();x.fill();x.stroke()});
    const T2=this.tally;
    if(bs.length||T2.w+T2.l){const m1='▲'+qU+' ▼'+qD+'  now '+(pn>=0?'+':'−')+'$'+Math.abs(pn).toFixed(2),m2='session '+T2.w+'W-'+T2.l+'L '+(T2.pnl>=0?'+':'−')+'$'+Math.abs(T2.pnl).toFixed(2);
      x.font='800 11px -apple-system,sans-serif';const bw=Math.max(x.measureText(m1).width,x.measureText(m2).width)+16;
      x.fillStyle='#050810d9';x.beginPath();x.roundRect(L+2,B-40,bw,34,8);x.fill();x.textAlign='left';x.fillStyle=pn>=0?G:R;x.fillText(m1,L+10,B-25);
      x.fillStyle='#7f8da3';x.font='700 10px -apple-system,sans-serif';x.fillText(m2,L+10,B-12)}
    // flame
    const n=Math.round(.4+this.int*3);
    for(let i=0;i<n;i++)this.parts.push({x:hx,y:hy,vx:-(1.2+Math.random()*2.2)*(1+this.int),vy:-Math.sign(this.vel||1)*Math.random()*1.4*this.int+(Math.random()-.5),l:1,c:col});
    x.globalCompositeOperation='lighter';
    for(let i=this.parts.length-1;i>=0;i--){const p=this.parts[i];p.x+=p.vx;p.y+=p.vy;p.l-=.035;if(p.l<=0){this.parts.splice(i,1);continue}
      x.globalAlpha=p.l*.8;x.fillStyle=p.c;x.beginPath();x.arc(p.x,p.y,.8+p.l*2.4,0,6.283);x.fill()}
    x.globalAlpha=1;x.globalCompositeOperation='source-over';
    for(let i=this.rings.length-1;i>=0;i--){const r=this.rings[i];r.r+=3.4;r.l-=.03;if(r.l<=0){this.rings.splice(i,1);continue}
      x.strokeStyle=r.c;x.globalAlpha=r.l;x.lineWidth=2.5;x.beginPath();x.arc(hx,hy,r.r,0,6.283);x.stroke();x.globalAlpha=1}
    // head glow, AI halo (arc = model P(up)), rocket tilted by momentum
    const pulse=(Math.sin(now*(5+this.int*14))+1)/2;
    x.shadowColor=col;x.shadowBlur=16+this.int*34;x.fillStyle=col;x.beginPath();x.arc(hx,hy,5+this.int*3,0,6.283);x.fill();x.shadowBlur=0;
    x.strokeStyle=col;x.globalAlpha=1-pulse;x.lineWidth=2;x.beginPath();x.arc(hx,hy,8+pulse*(14+this.int*14),0,6.283);x.stroke();x.globalAlpha=1;
    this.ang+=(Math.max(-1,Math.min(1,Math.atan2(-this.vel*sc*.6,14)))-this.ang)*.15;
    x.beginPath();x.arc(hx,hy,17,0,6.283);x.strokeStyle='#ffffff14';x.lineWidth=2;x.stroke();
    x.beginPath();x.arc(hx,hy,17,-1.5708,-1.5708+6.283*pc);x.strokeStyle=C;x.lineWidth=2.5;x.stroke();
    x.save();x.translate(hx,hy);x.rotate(this.ang);
    const fl=9+this.int*26+Math.random()*5,fg=x.createLinearGradient(-8,0,-8-fl,0);fg.addColorStop(0,'#fff');fg.addColorStop(.35,col);fg.addColorStop(1,'transparent');
    x.fillStyle=fg;x.beginPath();x.moveTo(-8,-3.2);x.lineTo(-8-fl,0);x.lineTo(-8,3.2);x.closePath();x.fill();
    x.fillStyle=col;x.beginPath();x.moveTo(-8,-4);x.lineTo(-13,-9);x.lineTo(-3,-4);x.closePath();x.fill();x.beginPath();x.moveTo(-8,4);x.lineTo(-13,9);x.lineTo(-3,4);x.closePath();x.fill();
    x.fillStyle='#eef3fb';x.beginPath();x.moveTo(-8,-4);x.quadraticCurveTo(5,-6,14,0);x.quadraticCurveTo(5,6,-8,4);x.closePath();x.fill();
    x.fillStyle=C;x.beginPath();x.arc(2,0,2,0,6.283);x.fill();x.restore();
    // tip pill
    const d=this.dispM-this.strike;
    const l1=(d>=0?'🚀 UP +':'🔻 DOWN −')+'$'+Math.abs(d).toFixed(2),l2='Model '+Math.round(pc*100)+'% UP';
    x.font='800 12px -apple-system,sans-serif';const w=Math.max(x.measureText(l1).width,x.measureText(l2).width)+18;
    let px=hx-w-24;if(px<L+2)px=Math.min(hx+24,W-w-4);const py=Math.min(Math.max(hy-50,T),B-44);
    x.fillStyle='#050810e6';x.strokeStyle=col;x.lineWidth=1.2;x.beginPath();x.roundRect(px,py,w,38,9);x.fill();x.stroke();
    x.textAlign='left';x.fillStyle=col;x.fillText(l1,px+9,py+16);x.fillStyle=C;x.font='700 11px -apple-system,sans-serif';x.fillText(l2,px+9,py+31);
    // round progress
    const pr=(now-this.ws)/WIN;x.fillStyle='#ffffff10';x.fillRect(0,H-3,W,3);
    x.fillStyle=rem<30?R:rem<120?AM:C;x.fillRect(0,H-3,W*Math.min(1,pr),3);
    if(this.res&&now<this.res.until){const a=Math.min(1,(this.res.until-now)/1.5),c=this.res.up?G:R;
      x.fillStyle=c;x.globalAlpha=.1*a;x.fillRect(0,0,W,H);x.globalAlpha=a;x.textAlign='center';x.font='900 30px -apple-system,sans-serif';
      x.fillText(this.res.up?'CLOSED UP':'CLOSED DOWN',(L+Rr)/2,H/2);
      if(this.res.n){x.font='800 15px -apple-system,sans-serif';x.fillStyle='#fff';x.fillText(this.res.n+' bets · '+(this.res.pnl>=0?'+':'−')+'$'+Math.abs(this.res.pnl).toFixed(2),(L+Rr)/2,H/2+26)}
      x.globalAlpha=1}
    if(DEBUG){x.textAlign='left';x.font='700 10px ui-monospace,monospace';x.fillStyle='#ffd166';
      [`raw ${this.raw.toFixed(2)}  ticks ${this.nt}  rejected ${this.rej}  age ${(now-this.lastT).toFixed(1)}s  tsSkew ${this.skewed}`,
       `strike ${this.strike.toFixed(2)} ${this.approx?'EST':'ok'}  bets ${bs.length}  off ${this.off}s  view ${this.view}`].forEach((s,i)=>x.fillText(s,10,36+i*12));
      const o=Object.values(charts).find(c=>c!==this);
      if(o&&o.strike!=null&&Math.abs(o.strike-this.strike)<.01&&Math.abs(o.dispM-this.dispM)>25){x.fillStyle='#ff6b6b';x.fillText('SAME STRIKE ON BOTH VENUES but prices differ $'+Math.abs(o.dispM-this.dispM).toFixed(0)+': strike is not from this venue feed',10,62)}}
    x.restore();
  }
}

function mountChart(host,key,opts){const c=new Chart(key,host);if(opts&&opts.view)c.view=+opts.view;charts[key]=c;
  if(!opts||opts.tapToCycle!==false)host.addEventListener('click',()=>c.setView(VIEWS[(VIEWS.indexOf(c.view)+1)%VIEWS.length]));return c}
const root=document.getElementById('rocket-root'),only=qs.get('venue');
if(root&&!window.ROCKET_NO_AUTOMOUNT)(only&&VENUES[only]?[only]:['polymarket','kalshi']).forEach(k=>{const h=document.createElement('div');h.className='c';root.appendChild(h);mountChart(h,k)});
window.RocketChart={mount:mountChart,charts,VIEWS};


const each=(v,fn)=>(v?[charts[v]]:Object.values(charts)).forEach(c=>c&&fn(c));
window.rocket={
  tick:(venue,price,ts)=>each(venue,c=>c.tick(price,ts)),            // venue: 'polymarket' | 'kalshi'. null feeds BOTH (legacy, avoid)
  setStrike:(venue,windowStartSec,price)=>each(venue,c=>c.setStrike(windowStartSec,price)),
  seed:(venue,ticks)=>each(venue,c=>c.seed(ticks)),                   // ticks: [{t,p}] oldest first
  setModel:(venue,pUp)=>each(venue,c=>{c.pOver=pUp}),
  syncClock:(serverMs)=>{clockOff=serverMs-Date.now()},
  bet:(venue,b)=>{if(!venue||!b||b.id==null)return;const ts=normTs(b.ts)||nowS();
    each(venue,c=>c.addBet({id:String(b.id),side:String(b.side).toUpperCase()==='DOWN'?'DOWN':'UP',ts,qty:+b.qty||0,cents:b.cents==null?null:+b.cents,status:b.status||'open',bot:b.bot||null}))},
  seedBets:(venue,arr)=>(arr||[]).forEach(b=>window.rocket.bet(venue,b)),
  setOffset:(venue,sec)=>each(venue,c=>{c.off=sec}),
};
// Merge the lowercase `rocket` hooks (tick/seed/setStrike/setModel/bet/syncClock/
// setOffset) onto `RocketChart` so callers can use a single global. The page's
// inline script calls `RocketChart.seed(...)` etc., which previously only lived
// on `window.rocket` — leaving the chart mounted but never fed.
Object.assign(window.RocketChart, window.rocket);
window.tick=p=>window.rocket.tick(null,p);
if(qs.get('kOff')&&charts.kalshi)charts.kalshi.off=+qs.get('kOff');
if(qs.get('pOff')&&charts.polymarket)charts.polymarket.off=+qs.get('pOff');

const qws=qs.get('ws');
if(qws){try{const ws=new WebSocket(qws);ws.onmessage=m=>{try{const d=JSON.parse(m.data);
  if(d.type==='tick')window.rocket.tick(d.venue||null,d.price,d.ts);
  else if(d.type==='strike')window.rocket.setStrike(d.venue||null,d.windowStartSec,d.price);
  else if(d.type==='model')window.rocket.setModel(d.venue||null,d.pUp);
  else if(d.type==='bet')window.rocket.bet(d.venue,d)}catch(_){}}}catch(_){}}
async function poll(){try{const r=await fetch(API+'/telemetry');if(!r.ok)return;const t=await r.json();
  if(t.serverTimeMs)window.rocket.syncClock(t.serverTimeMs);else if(t.serverTime)window.rocket.syncClock(t.serverTime*1000+500);
  if(typeof t.championProbUp==='number')window.rocket.setModel(null,t.championProbUp)}catch(e){}}
async function pollBets(){try{const r=await fetch(API+'/bets');if(!r.ok)return;const j=await r.json();
  ['kalshi','polymarket'].forEach(v=>(j[v]||[]).forEach(b=>window.rocket.bet(v,b)))}catch(e){}}
setInterval(poll,5000);poll();setInterval(pollBets,3000);pollBets();

// demo feed (only if no real ticks arrive within 2.5s). Polymarket trails Kalshi ~1s here to show the effect.
let base=77441.71,dr=0,bh=[];const lagBase=()=>{bh.push(base);return bh.length>8?bh.shift():bh[0]};
if(!window.ROCKET_NO_DEMO)setTimeout(()=>{if(!realSeen)demoOn=true},2500);
setInterval(()=>{if(!demoOn)return;dr=dr*.92+randn()*.5;let s=dr+randn()*1.1;if(Math.random()<.03)s+=(Math.random()<.5?-1:1)*(15+Math.random()*28);base+=s;
  if(charts.polymarket)charts.polymarket.tick(lagBase()+randn()*.6,null,true)},140);
setInterval(()=>{if(demoOn&&charts.kalshi)charts.kalshi.tick(base-2.5+randn()*.4,null,true)},1000);
setInterval(()=>{if(!demoOn)return;['kalshi','polymarket'].forEach(v=>{if(charts[v]&&Math.random()<.6)window.rocket.bet(v,{id:'d'+Math.random(),side:Math.random()<.5?'UP':'DOWN',ts:nowS(),qty:5+Math.floor(Math.random()*20),cents:40+Math.floor(Math.random()*30),bot:'demo'})})},9000);

let last=performance.now();
(function frame(t){const dt=Math.min(.05,(t-last)/1000);last=t;Object.values(charts).forEach(c=>c.step(dt));requestAnimationFrame(frame)})(last);
})();
