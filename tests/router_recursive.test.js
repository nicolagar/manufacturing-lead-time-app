
const assert = require('assert');
const { buildRecursiveRouteModel } = require('../static/router.js');
const { validateRecursiveRouteModel } = require('../static/router_validator.js');

const data = {
  schedule: [
    {process:'F0', refines:'', earliest_start:0, earliest_finish:1, duration:1},
    {process:'F1', refines:'', earliest_start:1, earliest_finish:2, duration:1},
    {process:'F4', refines:'', earliest_start:2, earliest_finish:6, duration:4},
    {process:'F5', refines:'', earliest_start:6, earliest_finish:7, duration:1},

    {process:'F4_0', refines:'F4', earliest_start:2, earliest_finish:3, duration:1},
    {process:'F4_1', refines:'F4', earliest_start:3, earliest_finish:5, duration:2},
    {process:'F4_2', refines:'F4', earliest_start:5, earliest_finish:6, duration:1},

    {process:'F4_1_0', refines:'F4_1', earliest_start:3, earliest_finish:4, duration:1},
    {process:'F4_1_1', refines:'F4_1', earliest_start:4, earliest_finish:5, duration:1},
  ],
  graph: {
    edges: [
      {from:'F0', to:'F1'},
      {from:'F1', to:'F4'},
      {from:'F4', to:'F5'},

      {from:'F4_0', to:'F4_1'},
      {from:'F4_1', to:'F4_2'},

      {from:'F4_1_0', to:'F4_1_1'}
    ]
  },
  critical_edges: [{from:'F1',to:'F4'}],
  dominant_path: ['F0','F1','F4','F5']
};

const model = buildRecursiveRouteModel(data);
const report = validateRecursiveRouteModel(model);
assert.strictEqual(report.ok, true, report.issues.join('; '));

// top level nodes present
['F0','F1','F4','F5'].forEach(id=>assert.ok(model.positions[id], `missing top node ${id}`));
// child nodes present
['F4_0','F4_1','F4_2','F4_1_0','F4_1_1'].forEach(id=>assert.ok(model.positions[id], `missing nested node ${id}`));
// parent containers present
['F4','F4_1'].forEach(id=>assert.ok(model.containers[id], `missing container ${id}`));

// nested nodes should be geometrically inside their parent containers
function inside(node, box){
  return node.x >= box.x && node.y >= box.y && node.x + node.w <= box.x + box.w && node.y + node.h <= box.y + box.h;
}
assert.ok(inside(model.positions['F4_0'], model.containers['F4']));
assert.ok(inside(model.positions['F4_1'], model.containers['F4']));
assert.ok(inside(model.positions['F4_1_0'], model.containers['F4_1']));
assert.ok(inside(model.positions['F4_1_1'], model.containers['F4_1']));

console.log(JSON.stringify({passed:1,total:1}));
