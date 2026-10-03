// Simple drainage allocation model (steady state, one time step).
// water to remove within the horizon H (h):  q = (C·P·A + S·excess)/H + base·A      (m3/s)
//   P = rain depth (mm) still to be drained, A = catchment (km2), S = canal water surface (m2), excess = level above warning
// route q through canals (capacity = cap0·v/V0) to pumps / boundary outlets at minimum cost
// cost: canal length (per 500 m) + pump 1 + outlet 3 ; water that cannot be routed = "unmet" (flood risk) cost 1e6
function runModel(M, prm){
  const N=M.net.nodes, E=M.net.edges, n=N.length, S=n, T=n+1;
  const drained=new Uint8Array(n); M.net.drained.forEach(i=>drained[i]=1);
  // rain intensity per gauge (mm/h)
  const gRate={};
  for(const [code,r] of Object.entries(M.rain)){
    let v=prm.rainMode==='rf1h'?r[3]:prm.rainMode==='rf3h'?r[4]:prm.rainMode==='rf24h'?r[5]:prm.rainMm;
    if(prm.gaugeMm)v=prm.gaugeMm[code]??null;
    gRate[code]=v==null?null:v;
  }
  const q=new Float64Array(n), parts={rain:0,base:0,store:0}, rainNode=new Float64Array(n), exNode=new Float64Array(n);
  for(let i=0;i<n;i++){const [lon,lat,A,Sf,d,st,rg]=N[i];
    let wsum=0,r=0; for(const [c,w] of rg){const v=prm.rainMode==='uniform'?prm.rainMm:gRate[c];if(v!=null){r+=w*v;wsum+=w}}
    r=wsum?r/wsum:0; if(prm.onlyDist&&!prm.onlyDist.includes(d))r=0; rainNode[i]=r;
    const qr=prm.C*r/1000*A*1e6/(prm.T*3600), qb=prm.base*A;
    let qs=0; if(st&&prm.useStore){const s=M.st[st];if(s&&s[1]!=null&&s[2]!=null&&s[4]!=='ขัดข้อง'){const ex=Math.max(0,s[1]-s[2]);exNode[i]=ex;qs=Sf*ex/(prm.T*3600)}}
    q[i]=qr+qb+qs; parts.rain+=qr; parts.base+=qb; parts.store+=qs;
  }
  const g=new MCF(n+2);
  const srcArc=new Int32Array(n).fill(-1), floodArc=new Int32Array(n).fill(-1);
  for(let i=0;i<n;i++) if(q[i]>1e-9){srcArc[i]=g.add(S,i,q[i],0); floodArc[i]=g.add(i,T,q[i],1e6)}
  const eArc=[]; const vr=prm.v/M.net.V0;
  for(let k=0;k<E.length;k++){const [u,v,nm,c,L,cap0]=E[k];const cap=cap0*vr*(prm.edgeMul&&prm.edgeMul[k]!=null?prm.edgeMul[k]:1);const cst=Math.max(1,Math.round(L/500));
    eArc.push([g.add(u,v,cap,cst),g.add(v,u,cap,cst),cap])}
  const pArc=M.net.pumps.map(p=>{const off=prm.pumpOff&&prm.pumpOff[p.id];if(p.internal||off)return null;const cap=p.cap*prm.avail;return [g.add(p.node,T,cap,1),cap]});
  const oArc=M.net.outlets.map(o=>prm.outletCap>0?[g.add(o.node,T,prm.outletCap,3),prm.outletCap]:null);
  const t0=(typeof performance!=='undefined'?performance:Date).now();
  const res=g.solve(S,T);
  const ms=(typeof performance!=='undefined'?performance:Date).now()-t0;
  const f=k=>res.arcFlow[k>>1];
  const edgeFlow=eArc.map(([a,b,cap])=>{const x=f(a)-f(b);return [x,cap]});   // + = u->v
  const pumpFlow=pArc.map(a=>a?[f(a[0]),a[1]]:null);
  const outFlow=oArc.map(a=>a?[f(a[0]),a[1]]:null);
  const unmet=new Float64Array(n); let unmetTot=0; for(let i=0;i<n;i++) if(floodArc[i]>=0){unmet[i]=f(floodArc[i]);unmetTot+=unmet[i]}
  return {q,parts,qTot:parts.rain+parts.base+parts.store,edgeFlow,pumpFlow,outFlow,unmet,unmetTot,ms,phases:res.phases,rainNode,exNode};
}
if(typeof module!=='undefined')module.exports={runModel};
