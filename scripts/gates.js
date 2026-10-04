// ---------- water gates (ประตูระบายน้ำ): status from BMA telemetry, advice from the levels on both sides ----------
// GATES rows: [code, name, lat, lon, opening_m, level_in, level_out, warn, crit, status, canal, time]
function gateAdvice(g){const [,,,,op,lin,lout,warn]=g;const open=op>0;
  if(lin==null||lout==null||lout<=-1.5)return {k:'na',open,t:'ไม่มีข้อมูลระดับน้ำด้านนอก'};
  const dh=lin-lout,high=warn!=null&&lin>=warn-0.2;
  if(dh>=0.05){if(open)return {k:'ok',open,dh,t:`เปิดอยู่ ${op} ม. น้ำด้านในสูงกว่า ${dh.toFixed(2)} ม. ระบายออกได้`};
    return high?{k:'open',open,dh,t:`ควรเปิด: น้ำด้านในสูงกว่าด้านนอก ${dh.toFixed(2)} ม. และใกล้/เกินเกณฑ์เตือน`}:{k:'ok',open,dh,t:`ปิดอยู่ เปิดได้ถ้าต้องการพร่องน้ำ (ด้านในสูงกว่า ${dh.toFixed(2)} ม.)`}}
  if(dh<=-0.05){if(open)return high?{k:'close',open,dh,t:`ควรหรี่/ปิด: น้ำด้านนอกสูงกว่า ${(-dh).toFixed(2)} ม. กำลังไหลเข้าพื้นที่ที่ระดับใกล้/เกินเกณฑ์เตือน`}:{k:'ok',open,dh,t:`เปิดรับน้ำอยู่ ${op} ม. (ด้านนอกสูงกว่า ${(-dh).toFixed(2)} ม. ด้านในยังต่ำกว่าเกณฑ์)`};
    return {k:'ok',open,dh,t:`ปิดไว้ กันน้ำด้านนอกที่สูงกว่า ${(-dh).toFixed(2)} ม.`}}
  return {k:'ok',open,dh,t:`ระดับสองฝั่งใกล้กัน (${dh>=0?'+':''}${dh.toFixed(2)} ม.) เปิดไม่ช่วยระบาย`}}
const GATE_COL={open:'var(--accent)',close:'var(--crit)',ok:'var(--ink-2)',na:'var(--axis)'};
function drawGates(G,layer,P,showTip,hideTip,esc,onClick){layer.innerHTML='';const NS='http://www.w3.org/2000/svg';
  const mk=(t,a,p)=>{const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);p.appendChild(e);return e};
  G.forEach(g=>{const a=gateAdvice(g);const [x,y]=P([g[3],g[2]]);const gg=mk('g',{'data-x':x.toFixed(1),'data-y':y.toFixed(1),tabindex:0,style:'cursor:pointer'},layer);
    // a bar across the canal: filled = open, hollow = closed; colour = advice
    mk('rect',{x:-5,y:-2.2,width:10,height:4.4,rx:1,fill:a.open?GATE_COL[a.k]:'var(--surface)',stroke:GATE_COL[a.k],'stroke-width':1.6},gg);
    if(a.k==='open'||a.k==='close')mk('circle',{r:8,fill:'none',stroke:GATE_COL[a.k],'stroke-width':1.2,'stroke-dasharray':'2 2'},gg);
    gg.addEventListener('mousemove',e=>showTip(e,`<b>${esc(g[1])}</b><br>${a.open?`เปิด ${g[4]} ม.`:'ปิด'}<br>ใน ${g[5]??'–'} / นอก ${g[6]!=null&&g[6]>-1.5?g[6]:'–'} ม.รทก.<br>เตือน ${g[7]??'–'}<br>${esc(a.t)}`));
    gg.addEventListener('mouseleave',hideTip);if(onClick)gg.addEventListener('click',()=>onClick(g,a))})}
function gateSummary(G){const c={open:[],close:[],ok:[],na:[]};G.forEach(g=>{const a=gateAdvice(g);c[a.k].push([g,a])});return c}
