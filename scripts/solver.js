// Min-cost flow (primal–dual: Dijkstra with potentials + Dinic blocking flow on zero-reduced-cost arcs).
// Integer costs, real capacities. Shared by bkk_model.html (inlined) and the Node test.
function MCF(n){
  this.n=n; this.head=new Int32Array(n).fill(-1);
  this.to=[]; this.nx=[]; this.cap=[]; this.cost=[];
}
MCF.prototype.add=function(u,v,cap,cost){
  const k=this.to.length;
  this.to.push(v,u); this.cap.push(cap,0); this.cost.push(cost,-cost);
  this.nx.push(this.head[u],this.head[v]); this.head[u]=k; this.head[v]=k+1; return k;
};
MCF.prototype.solve=function(s,t){
  const n=this.n,to=Int32Array.from(this.to),nx=Int32Array.from(this.nx),cost=Float64Array.from(this.cost),head=this.head;
  const cap=Float64Array.from(this.cap), orig=Float64Array.from(this.cap); const EPS=1e-9;
  const pot=new Float64Array(n), dist=new Float64Array(n), lvl=new Int32Array(n), it=new Int32Array(n);
  let flow=0, tot=0, phases=0;
  // binary heap
  const hk=new Float64Array(this.to.length+n+5), hv=new Int32Array(this.to.length+n+5);
  while(true){
    phases++; dist.fill(Infinity); dist[s]=0; let hs=0;
    const push=(k,v)=>{let i=hs++;while(i>0){const p=(i-1)>>1;if(hk[p]<=k)break;hk[i]=hk[p];hv[i]=hv[p];i=p}hk[i]=k;hv[i]=v};
    const pop=()=>{const v=hv[0],k=hk[0];hs--;const lk=hk[hs],lv=hv[hs];let i=0;while(true){let c=2*i+1;if(c>=hs)break;if(c+1<hs&&hk[c+1]<hk[c])c++;if(hk[c]>=lk)break;hk[i]=hk[c];hv[i]=hv[c];i=c}hk[i]=lk;hv[i]=lv;return [k,v]};
    push(0,s);
    while(hs){const [d,u]=pop(); if(d>dist[u])continue;
      for(let e=head[u];e!==-1;e=nx[e]){if(cap[e]<=EPS)continue;const v=to[e];const nd=d+cost[e]+pot[u]-pot[v];if(nd<dist[v]){dist[v]=nd;push(nd,v)}}}
    if(dist[t]===Infinity)break;
    const dt=dist[t]; for(let v=0;v<n;v++) pot[v]+=Math.min(dist[v],dt);
    // Dinic on admissible arcs (reduced cost == 0)
    const adm=e=>cap[e]>EPS && cost[e]+pot[to[e^1]]-pot[to[e]]===0;
    while(true){
      lvl.fill(-1); lvl[s]=0; const q=[s];
      for(let qi=0;qi<q.length;qi++){const u=q[qi];for(let e=head[u];e!==-1;e=nx[e]){if(lvl[to[e]]<0&&adm(e)){lvl[to[e]]=lvl[u]+1;q.push(to[e])}}}
      if(lvl[t]<0)break;
      for(let v=0;v<n;v++)it[v]=head[v];
      const dfs=(u,f)=>{ if(u===t)return f;
        for(;it[u]!==-1;it[u]=nx[it[u]]){const e=it[u],v=to[e];
          if(lvl[v]===lvl[u]+1&&adm(e)){const d=dfs(v,Math.min(f,cap[e]));if(d>EPS){cap[e]-=d;cap[e^1]+=d;return d}}}
        return 0};
      let f; while((f=dfs(s,Infinity))>EPS){flow+=f;}
    }
    if(phases>5000)break;
  }
  // flows on forward arcs
  const fl=new Float64Array(this.to.length/2); for(let k=0;k<fl.length;k++){fl[k]=orig[2*k]-cap[2*k]; tot+=fl[k]*cost[2*k]}
  return {flow,cost:tot,phases,arcFlow:fl};
};
if(typeof module!=='undefined')module.exports={MCF};
