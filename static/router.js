
(function(global){
  function simplifyPoints(points){
    const out=[points[0]];
    for(let i=1;i<points.length;i++){
      const p=points[i], q=out[out.length-1];
      if(p.x!==q.x || p.y!==q.y) out.push(p);
    }
    return out;
  }
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

  function buildHierarchy(schedule){
    const byParent={};
    (schedule||[]).forEach(r=>{
      const p=r.refines||'';
      (byParent[p]||(byParent[p]=[])).push(r);
    });
    Object.values(byParent).forEach(arr=>arr.sort((a,b)=>(a.earliest_start??0)-(b.earliest_start??0)||String(a.process).localeCompare(String(b.process))));
    return byParent;
  }

  function edgesForParent(graph, scheduleMap, parent){
    return (graph.edges||[]).filter(e=>(scheduleMap[e.from]?.refines||'')===parent && (scheduleMap[e.to]?.refines||'')===parent);
  }

  function computeLevels(rows, edges){
    const ids=rows.map(r=>r.process), incoming={}, outgoing={};
    ids.forEach(id=>{incoming[id]=[]; outgoing[id]=[];});
    edges.forEach(e=>{ if(incoming[e.to]&&outgoing[e.from]){ incoming[e.to].push(e.from); outgoing[e.from].push(e.to); } });
    const levels={}, queue=ids.filter(id=>incoming[id].length===0).sort();
    queue.forEach(id=>levels[id]=0);
    while(queue.length){
      const id=queue.shift();
      outgoing[id].forEach(next=>{
        levels[next]=Math.max(levels[next]||0,(levels[id]||0)+1);
        incoming[next]=incoming[next].filter(x=>x!==id);
        if(incoming[next].length===0) queue.push(next);
      });
    }
    let changed=true;
    while(changed){
      changed=false;
      edges.forEach(e=>{
        const a=levels[e.from]??0, b=levels[e.to]??0;
        if(b<=a){levels[e.to]=a+1; changed=true;}
      });
    }
    ids.forEach(id=>{ if(levels[id]===undefined) levels[id]=0; });
    return levels;
  }

  function computeLanes(rows, edges, levels){
    const byLevel={}; rows.forEach(r=>{const lvl=levels[r.process]||0;(byLevel[lvl]||(byLevel[lvl]=[])).push(r);});
    const laneOf={}, used={};
    Object.keys(byLevel).map(Number).sort((a,b)=>a-b).forEach(level=>{
      byLevel[level].sort((a,b)=>(a.earliest_start??0)-(b.earliest_start??0)||String(a.process).localeCompare(String(b.process)));
      byLevel[level].forEach(row=>{
        const preds=edges.filter(e=>e.to===row.process).map(e=>e.from).filter(p=>laneOf[p]!==undefined);
        let pref=preds.length?Math.round(preds.reduce((s,p)=>s+laneOf[p],0)/preds.length):0;
        let lane=pref;
        while((used[level]||new Set()).has(lane)) lane++;
        (used[level]||(used[level]=new Set())).add(lane);
        laneOf[row.process]=lane;
      });
    });
    return laneOf;
  }

  function rightMid(p){return {x:p.x+p.w,y:p.y+p.h/2};}
  function leftMid(p){return {x:p.x,y:p.y+p.h/2};}
  function topPoint(p,slot,total){
    const span=Math.max(24,p.w*0.6), start=p.x+(p.w-span)/2, step=total>1?span/(total-1):0;
    return {x:start+slot*step,y:p.y};
  }
  function bottomPoint(p,slot,total){
    const span=Math.max(24,p.w*0.6), start=p.x+(p.w-span)/2, step=total>1?span/(total-1):0;
    return {x:start+slot*step,y:p.y+p.h};
  }

  function buildRecursiveRouteModel(data){
    const schedule=data.schedule||[];
    const scheduleMap={}; schedule.forEach(r=>scheduleMap[r.process]=r);
    const graph=data.graph||{edges:[]};
    const byParent=buildHierarchy(schedule);

    const dominantEdges=new Set((data.critical_edges||[]).map(e=>`${e.from}__${e.to}`));
    const dominantPath=new Set(data.dominant_path||[]);

    const positions={};          // process -> rect
    const containers={};         // process -> container rect
    const routes=[];             // all routes
    const nodeParent={};         // process -> parent process or ''
    const metadata={};           // process -> row
    schedule.forEach(r=>{metadata[r.process]=r;});

    function layoutNetwork(parentKey, originX, originY, depth){
      const rows=(byParent[parentKey]||[]).slice();
      if(!rows.length) return {w:0,h:0};
      const edges=edgesForParent(graph, scheduleMap, parentKey);
      const levels=computeLevels(rows, edges);
      const lanes=computeLanes(rows, edges, levels);

      const cardW=Math.max(124, 176 - depth*10);
      const cardH=Math.max(54, 76 - depth*6);
      const colGap=depth===0 ? 180 : 110;
      const laneGap=depth===0 ? 90 : 54;
      const marginX=depth===0 ? 60 : 26;
      const marginY=depth===0 ? 50 : 22;
      const headerH=28;
      const pad=18;

      let maxX=0, maxY=0;

      rows.forEach(r=>{
        const x=originX+marginX+(levels[r.process]||0)*(cardW+colGap);
        const y=originY+marginY+(lanes[r.process]||0)*(cardH+laneGap);
        positions[r.process]={x,y,w:cardW,h:cardH,depth,parent:parentKey};
        nodeParent[r.process]=parentKey;
        maxX=Math.max(maxX,x+cardW);
        maxY=Math.max(maxY,y+cardH);
      });

      // layout child containers recursively; child network visible inside parent container
      rows.forEach(r=>{
        const childRows=(byParent[r.process]||[]).slice();
        if(!childRows.length) return;
        const node=positions[r.process];
        const childOriginX=node.x+12;
        const childOriginY=node.y+node.h+headerH;
        const childBox=layoutNetwork(r.process, childOriginX, childOriginY, depth+1);
        const box={
          x:node.x-14,
          y:node.y-14,
          w:Math.max(node.w+28, (childOriginX-(node.x-14)) + childBox.w + 18),
          h:Math.max(node.h+28, (childOriginY-(node.y-14)) + childBox.h + 18),
          headerH: headerH
        };
        containers[r.process]=box;
        maxX=Math.max(maxX, box.x+box.w);
        maxY=Math.max(maxY, box.y+box.h);
      });

      // route edges only after all nodes/containers at this level exist
      const incomingByTarget={};
      edges.forEach(e=>{ (incomingByTarget[e.to]||(incomingByTarget[e.to]=[])).push(e); });

      const assignment={};
      Object.entries(incomingByTarget).forEach(([target, arr])=>{
        const tgt=positions[target];
        const tgtCy=tgt.y+tgt.h/2;
        const classified={top:[], left:[], bottom:[]};
        arr.forEach(edge=>{
          const src=positions[edge.from];
          const srcCy=src.y+src.h/2;
          if(arr.length===1) classified.left.push(edge);
          else if(srcCy < tgtCy - 20) classified.top.push(edge);
          else if(srcCy > tgtCy + 20) classified.bottom.push(edge);
          else classified.left.push(edge);
        });
        if(arr.length>1 && classified.left.length===0){
          const donor = classified.top.length > classified.bottom.length ? 'top' : 'bottom';
          if(classified[donor].length) classified.left.push(classified[donor].shift());
        }
        ['top','left','bottom'].forEach(side=>{
          classified[side].sort((a,b)=>{
            const aCrit=dominantEdges.has(`${a.from}__${a.to}`)?-1:0;
            const bCrit=dominantEdges.has(`${b.from}__${b.to}`)?-1:0;
            if(aCrit!==bCrit) return aCrit-bCrit;
            return positions[a.from].y-positions[b.from].y;
          });
          const total=classified[side].length;
          classified[side].forEach((edge,idx)=>assignment[`${edge.from}__${edge.to}`]={side,slot:idx,total});
        });
      });

      function internalObstacleRects(parent){
        return rows
          .filter(r=>r.process!==parent)
          .map(r=>positions[r.process])
          .concat(
            rows.filter(r=>containers[r.process]).map(r=>{
              const c=containers[r.process];
              return {x:c.x,y:c.y,w:c.w,h:c.h};
            })
          );
      }

      function routeEdge(edge){
        const from=positions[edge.from], to=positions[edge.to];
        const a=assignment[`${edge.from}__${edge.to}`] || {side:'left',slot:0,total:1};
        const start=rightMid(from); // outgoing always right
        let end;
        if(a.side==='left') end=leftMid(to);
        else if(a.side==='top') end=topPoint(to,a.slot,a.total);
        else end=bottomPoint(to,a.slot,a.total);

        const targetApproachGap=46;
        const sourceRun=34;
        const slotDelta=16;
        let pts=[start];

        if(a.side==='left'){
          const approachX=end.x-targetApproachGap-a.slot*slotDelta;
          const bendX=Math.max(start.x+sourceRun, approachX-48);
          pts.push({x:bendX,y:start.y});
          pts.push({x:approachX,y:start.y});
          pts.push({x:approachX,y:end.y});
          pts.push(end);
        } else if(a.side==='top'){
          const approachY=end.y-targetApproachGap-a.slot*slotDelta;
          const bendX=Math.max(start.x+sourceRun, end.x-52+a.slot*8);
          pts.push({x:bendX,y:start.y});
          pts.push({x:bendX,y:approachY});
          pts.push({x:end.x,y:approachY});
          pts.push(end);
        } else {
          const approachY=end.y+targetApproachGap+a.slot*slotDelta;
          const bendX=Math.max(start.x+sourceRun, end.x-52+a.slot*8);
          pts.push({x:bendX,y:start.y});
          pts.push({x:bendX,y:approachY});
          pts.push({x:end.x,y:approachY});
          pts.push(end);
        }
        return simplifyPoints(pts);
      }

      const sortedEdges=[...edges].sort((a,b)=>{
        const aCrit=dominantEdges.has(`${a.from}__${a.to}`)?1:0;
        const bCrit=dominantEdges.has(`${b.from}__${b.to}`)?1:0;
        return aCrit-bCrit;
      });

      const localRoutes={};
      sortedEdges.forEach(e=>{ localRoutes[`${e.from}__${e.to}`]=routeEdge(e); });

      // basic repair loop for local crossings
      function validateLocal(){
        const keys=Object.keys(localRoutes);
        for(let i=0;i<keys.length;i++){
          for(let j=i+1;j<keys.length;j++){
            const s1=segs(localRoutes[keys[i]]);
            const s2=segs(localRoutes[keys[j]]);
            for(const a of s1){
              for(const b of s2){
                if(cross(a,b)) return {ok:false,a:keys[i],b:keys[j]};
              }
            }
          }
        }
        return {ok:true};
      }
      for(let repair=0; repair<10; repair++){
        const chk=validateLocal();
        if(chk.ok) break;
        const loser = dominantEdges.has(chk.b) ? chk.a : chk.b;
        const [fromId,toId]=loser.split('__');
        const cur=assignment[loser]||{slot:0};
        assignment[loser]={...(assignment[loser]||{side:'left',total:1}), slot:cur.slot+1};
        localRoutes[loser]=routeEdge({from:fromId,to:toId});
      }

      sortedEdges.forEach(e=>{
        routes.push({
          from:e.from,
          to:e.to,
          parent:parentKey,
          points:localRoutes[`${e.from}__${e.to}`]
        });
      });

      return {w:maxX-originX+30,h:maxY-originY+30};
    }

    const rootBox=layoutNetwork('', 0, 0, 0);
    return {
      positions,
      containers,
      routes,
      dominantEdges,
      dominantPath,
      scheduleMap,
      contentW: Math.max(rootBox.w+40, 1320),
      contentH: Math.max(rootBox.h+40, 720)
    };
  }

  const api = { buildRecursiveRouteModel };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  global.ProvenRouter = api;
})(typeof window !== 'undefined' ? window : globalThis);
