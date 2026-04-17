
(function(global){
  function segs(points){
    const out=[];
    for(let i=0;i<points.length-1;i++) out.push([points[i], points[i+1]]);
    return out;
  }
  function cross(s1,s2){
    const [a,b]=s1,[c,d]=s2;
    const aVert=a.x===b.x, cVert=c.x===d.x;
    if(aVert===cVert) return false;
    const vert=aVert?[a,b]:[c,d];
    const hor=aVert?[c,d]:[a,b];
    const vx=vert[0].x, hy=hor[0].y;
    const vMin=Math.min(vert[0].y,vert[1].y), vMax=Math.max(vert[0].y,vert[1].y);
    const hMin=Math.min(hor[0].x,hor[1].x), hMax=Math.max(hor[0].x,hor[1].x);
    return vx>hMin && vx<hMax && hy>vMin && hy<vMax;
  }
  function insideRectPoint(pt, rect, pad=0){
    return pt.x > rect.x+pad && pt.x < rect.x+rect.w-pad && pt.y > rect.y+pad && pt.y < rect.y+rect.h-pad;
  }

  function validateRecursiveRouteModel(model){
    const issues=[];
    const pos=model.positions;
    const containers=model.containers;
    const routeMap={};
    model.routes.forEach(r=>{ routeMap[`${r.from}__${r.to}`]=r.points; });

    // outgoing right
    model.routes.forEach(r=>{
      const node=pos[r.from];
      const first=r.points[0];
      if(first.x !== node.x + node.w) issues.push(`outgoing-not-right:${r.from}->${r.to}`);
    });

    // recursive containment: if route.parent != '' the route points must lie within that parent container content area
    model.routes.forEach(r=>{
      if(!r.parent) return;
      const c=containers[r.parent];
      if(!c) return;
      const content={x:c.x+8,y:c.y+c.headerH,w:c.w-16,h:c.h-c.headerH-8};
      r.points.forEach(pt=>{
        if(!insideRectPoint(pt, content, -1) && !(pt.x===pos[r.to].x || pt.x===pos[r.from].x+pos[r.from].w || pt.y===pos[r.to].y || pt.y===pos[r.to].y+pos[r.to].h)){
          issues.push(`internal-route-outside-content:${r.from}->${r.to}`);
        }
      });
    });

    // no crossings within same parent network
    for(let i=0;i<model.routes.length;i++){
      for(let j=i+1;j<model.routes.length;j++){
        if(model.routes[i].parent !== model.routes[j].parent) continue;
        for(const s1 of segs(model.routes[i].points)){
          for(const s2 of segs(model.routes[j].points)){
            if(cross(s1,s2)) issues.push(`crossing:${model.routes[i].from}->${model.routes[i].to}:${model.routes[j].from}->${model.routes[j].to}`);
          }
        }
      }
    }

    return {ok:issues.length===0, issues};
  }

  const api={validateRecursiveRouteModel};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.RouterValidator = api;
})(typeof window !== 'undefined' ? window : globalThis);
