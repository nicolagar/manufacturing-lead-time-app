from io import BytesIO
import ast
import json
import math
import re
from typing import Dict, List, Set, Tuple

import networkx as nx
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel


SHEET_NAME = "PERT"
FORMULA_SHEET_NAME = "FORMULA"
SAMPLE_ROWS = [
    {"process": "F00", "predecessor": "NA", "successor": "F01", "duration": "Lead_A", "unitary_value": 100, "additional": 10, "workers": 20, "p0": "", "p1": ""},
    {"process": "F01", "predecessor": "F00", "successor": "F02;F03", "duration": "Lead_B", "unitary_value": "", "additional": "", "workers": 10, "p0": "", "p1": ""},
    {"process": "F02", "predecessor": "F01", "successor": "F04", "duration": 107, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
    {"process": "F03", "predecessor": "F01", "successor": "F04", "duration": 111, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
    {"process": "F04", "predecessor": "F02;F03", "successor": "F05", "duration": 66, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
    {"process": "F05", "predecessor": "F04", "successor": "F07", "duration": 48, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
    {"process": "F06", "predecessor": "NA", "successor": "F07", "duration": 193, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
    {"process": "F07", "predecessor": "F05;F06", "successor": "STOP", "duration": 18, "unitary_value": "", "additional": "", "workers": "", "p0": "", "p1": ""},
]
SAMPLE_FORMULAS = [
    {"name": "Lead_A", "formula": "(unitary_value*additional)+workers"},
    {"name": "Lead_B", "formula": "workers*workers"},
]
PARAM_COLUMNS = [f"p{i}" for i in range(5)]


class PertDataError(Exception):
    pass


class CycleError(Exception):
    pass


class ProcessRow(BaseModel):
    process: str
    predecessor: str = "NA"
    successor: str = "STOP"
    duration: str | float | int
    unitary_value: str | float | int | None = None
    additional: str | float | int | None = None
    workers: str | float | int | None = None
    p0: str | float | int | None = None
    p1: str | float | int | None = None
    p2: str | float | int | None = None
    p3: str | float | int | None = None
    p4: str | float | int | None = None
    p5: str | float | int | None = None
    p6: str | float | int | None = None
    p7: str | float | int | None = None
    p8: str | float | int | None = None
    p9: str | float | int | None = None

    model_config = {"extra": "allow"}


class FormulaRow(BaseModel):
    name: str
    formula: str


class ComputeRequest(BaseModel):
    rows: List[ProcessRow]
    formulas: List[FormulaRow] | None = None


app = FastAPI(title="Manufacturing Lead Time Web App")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize_text(value) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def coerce_number(value):
    text = normalize_text(value)
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def split_logic_field(value: str, terminal_token: str) -> List[str]:
    text = normalize_text(value)
    if not text:
        return []
    if text.upper() == terminal_token:
        return []
    return [item.strip() for item in text.split(";") if item.strip()]


def build_formula_map(formula_df: pd.DataFrame) -> Dict[str, str]:
    if formula_df.empty:
        return {}
    first_two = formula_df.iloc[:, :2].copy()
    first_two.columns = ["name", "formula"]
    first_two["name"] = first_two["name"].map(normalize_text)
    first_two["formula"] = first_two["formula"].map(normalize_text)
    first_two = first_two[(first_two["name"] != "") & (first_two["formula"] != "")]
    return dict(zip(first_two["name"], first_two["formula"]))


def extract_formula_variables(expression: str) -> List[str]:
    tree = ast.parse(expression, mode="eval")
    return sorted({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)})


def safe_eval_formula(expression: str, variables: Dict[str, float]) -> float:
    allowed_funcs = {
        "abs": abs,
        "min": min,
        "max": max,
        "round": round,
        "ceil": math.ceil,
        "floor": math.floor,
        "sqrt": math.sqrt,
    }
    allowed_nodes = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.Pow, ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd, ast.Load, ast.Name,
        ast.Constant, ast.Call,
    )
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed_nodes):
            raise PertDataError(f"Unsupported formula syntax: {expression}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in allowed_funcs:
                raise PertDataError(f"Unsupported function in formula: {expression}")
        if isinstance(node, ast.Name):
            if node.id not in variables and node.id not in allowed_funcs:
                raise PertDataError(f"Missing variable '{node.id}' for formula: {expression}")
    return float(eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, {**allowed_funcs, **variables}))


def resolve_duration(row: pd.Series, formula_map: Dict[str, str], extra_columns: List[str]) -> float:
    raw_duration = row["duration_raw"]
    numeric_duration = coerce_number(raw_duration)
    if numeric_duration is not None:
        return numeric_duration

    token = normalize_text(raw_duration)
    if token == "":
        raise PertDataError(f"Missing duration for process '{row['process']}'.")
    if token not in formula_map:
        raise PertDataError(f"Duration token '{token}' for process '{row['process']}' was not found in sheet '{FORMULA_SHEET_NAME}'.")

    expression = formula_map[token]
    needed_vars = [name for name in extract_formula_variables(expression) if name not in {"abs", "min", "max", "round", "ceil", "floor", "sqrt"}]

    header_values = {col: coerce_number(row.get(col)) for col in extra_columns if re.fullmatch(r"[A-Za-z_]\w*", str(col))}
    positional_values = {f"p{i}": coerce_number(row.get(col)) for i, col in enumerate(extra_columns)}

    variables = {}
    missing = []
    for var in needed_vars:
        value = None
        if var in header_values and header_values[var] is not None:
            value = header_values[var]
        elif var in positional_values and positional_values[var] is not None:
            value = positional_values[var]
        if value is None:
            missing.append(var)
        else:
            variables[var] = value

    if missing:
        raise PertDataError(f"Missing parameter values {missing} for process '{row['process']}' using formula '{token}'.")
    return safe_eval_formula(expression, variables)


def validate_dataframe(df: pd.DataFrame) -> None:
    if df.empty:
        raise PertDataError("No process rows provided.")
    if df["process"].eq("").any():
        bad_rows = (df.index[df["process"].eq("")] + 1).tolist()
        raise PertDataError(f"Empty process name found in rows: {bad_rows}")
    if df["process"].duplicated().any():
        duplicates = df.loc[df["process"].duplicated(keep=False), "process"].tolist()
        raise PertDataError(f"Duplicate process names found: {sorted(set(duplicates))}")
    if df["duration"].isna().any():
        bad_rows = (df.index[df["duration"].isna()] + 1).tolist()
        raise PertDataError(f"Invalid or unresolved duration found in rows: {bad_rows}")
    if (df["duration"] < 0).any():
        bad_rows = (df.index[df["duration"] < 0] + 1).tolist()
        raise PertDataError(f"Negative duration found in rows: {bad_rows}")


def validate_references(df: pd.DataFrame) -> None:
    process_set = set(df["process"])
    invalid_predecessors = set()
    invalid_successors = set()
    for _, row in df.iterrows():
        for pred in split_logic_field(row["predecessor"], "NA"):
            if pred not in process_set:
                invalid_predecessors.add(pred)
        for succ in split_logic_field(row["successor"], "STOP"):
            if succ not in process_set:
                invalid_successors.add(succ)
    if invalid_predecessors:
        raise PertDataError(f"Unknown predecessor references: {sorted(invalid_predecessors)}")
    if invalid_successors:
        raise PertDataError(f"Unknown successor references: {sorted(invalid_successors)}")


def prepare_dataframe(df: pd.DataFrame, formula_map: Dict[str, str]) -> pd.DataFrame:
    for col in ["process", "predecessor", "successor", "duration_raw"]:
        if col not in df.columns:
            raise PertDataError(f"Missing required column: {col}")
    for col in ["process", "predecessor", "successor"]:
        df[col] = df[col].map(normalize_text)
    extra_columns = [col for col in df.columns if col not in ["process", "predecessor", "successor", "duration_raw"]]
    df["duration"] = df.apply(lambda row: resolve_duration(row, formula_map, extra_columns), axis=1)
    validate_dataframe(df)
    validate_references(df)
    return df


def dataframe_from_rows(rows: List[Dict], formula_map: Dict[str, str]) -> pd.DataFrame:
    df = pd.DataFrame(rows).rename(columns={"duration": "duration_raw"})
    return prepare_dataframe(df, formula_map)


def load_pert_excel_from_bytes(content: bytes) -> Tuple[pd.DataFrame, Dict[str, str]]:
    buffer = BytesIO(content)
    preview = pd.read_excel(buffer, sheet_name=SHEET_NAME, header=None)
    if preview.shape[1] < 4:
        raise PertDataError(f"Sheet '{SHEET_NAME}' must contain at least 4 columns: process, predecessor, successor, duration.")

    first_row = [normalize_text(x).lower() for x in preview.iloc[0, :4].tolist()]
    has_header = set(first_row) == {"process", "predecessor", "successor", "duration"}

    buffer.seek(0)
    if has_header:
        df = pd.read_excel(buffer, sheet_name=SHEET_NAME).copy()
        renamed = []
        for idx, col in enumerate(df.columns):
            if idx == 0:
                renamed.append("process")
            elif idx == 1:
                renamed.append("predecessor")
            elif idx == 2:
                renamed.append("successor")
            elif idx == 3:
                renamed.append("duration_raw")
            else:
                renamed.append(normalize_text(col) or f"p{idx-4}")
        df.columns = renamed
    else:
        df = preview.copy()
        base = ["process", "predecessor", "successor", "duration_raw"]
        extra = [f"p{i}" for i in range(max(0, df.shape[1] - 4))]
        df.columns = base + extra

    formula_df = pd.read_excel(BytesIO(content), sheet_name=FORMULA_SHEET_NAME, header=None)
    formula_map = build_formula_map(formula_df)
    return prepare_dataframe(df, formula_map), formula_map


def build_graph(df: pd.DataFrame) -> nx.DiGraph:
    g = nx.DiGraph()
    for _, row in df.iterrows():
        g.add_node(row["process"], duration=float(row["duration"]))
    for _, row in df.iterrows():
        for pred in split_logic_field(row["predecessor"], "NA"):
            g.add_edge(pred, row["process"])
        for succ in split_logic_field(row["successor"], "STOP"):
            g.add_edge(row["process"], succ)
    if not nx.is_directed_acyclic_graph(g):
        raise CycleError("The process network contains a cycle. Critical path calculation requires a DAG.")
    return g


def topological_order(g: nx.DiGraph) -> List[str]:
    return list(nx.topological_sort(g))


def compute_dominant_path(schedule_df: pd.DataFrame) -> List[str]:
    if schedule_df.empty:
        return []
    index = schedule_df.set_index("process")
    project_finish = float(schedule_df["earliest_finish"].max())
    sinks = sorted(schedule_df.loc[schedule_df["earliest_finish"] == project_finish, "process"].tolist())
    current = sinks[0] if sinks else None
    path = []
    while current:
        path.insert(0, current)
        row = index.loc[current]
        predecessor_candidates = []
        for pred in row["predecessors"]:
            pred_row = index.loc[pred]
            if pred_row["critical"] and abs(pred_row["earliest_finish"] - row["earliest_start"]) < 1e-9:
                predecessor_candidates.append((pred, pred_row["earliest_finish"]))
        predecessor_candidates.sort(key=lambda x: x[1], reverse=True)
        current = predecessor_candidates[0][0] if predecessor_candidates else None
    return path


def compute_dominant_edges(g: nx.DiGraph, schedule_df: pd.DataFrame, dominant_set: Set[str]) -> Set[Tuple[str, str]]:
    index = schedule_df.set_index("process")
    critical_edges = set()
    for u, v in g.edges():
        if u in dominant_set and v in dominant_set:
            if abs(index.loc[u, "earliest_finish"] - index.loc[v, "earliest_start"]) < 1e-9:
                critical_edges.add((u, v))
    return critical_edges


def compute_schedule(df: pd.DataFrame) -> Dict:
    g = build_graph(df)
    duration = nx.get_node_attributes(g, "duration")
    order = topological_order(g)
    indegrees = {node: g.in_degree(node) for node in g.nodes}
    outdegrees = {node: g.out_degree(node) for node in g.nodes}

    es: Dict[str, float] = {}
    ef: Dict[str, float] = {}
    for node in order:
        preds = list(g.predecessors(node))
        es[node] = max((ef[p] for p in preds), default=0.0)
        ef[node] = es[node] + duration[node]

    for node in order:
        if list(g.predecessors(node)):
            continue
        succs = list(g.successors(node))
        multi_pred_succs = [s for s in succs if indegrees[s] > 1]
        feasible_candidates = [es[succ] - duration[node] for succ in multi_pred_succs if es[succ] - duration[node] >= 0]
        if feasible_candidates:
            es[node] = min(feasible_candidates)
            ef[node] = es[node] + duration[node]

    for node in order:
        preds = list(g.predecessors(node))
        if preds:
            es[node] = max(ef[p] for p in preds)
        ef[node] = es[node] + duration[node]

    for node in order:
        succs = list(g.successors(node))
        preds = list(g.predecessors(node))
        if succs or len(preds) != 1:
            continue
        pred = preds[0]
        if outdegrees[pred] > 1:
            es[node] = max(es[node], ef[pred])
            ef[node] = es[node] + duration[node]

    for node in order:
        preds = list(g.predecessors(node))
        if preds:
            es[node] = max(ef[p] for p in preds)
        ef[node] = es[node] + duration[node]

    project_finish = max(ef.values()) if ef else 0.0
    ls: Dict[str, float] = {}
    lf: Dict[str, float] = {}
    for node in reversed(order):
        succs = list(g.successors(node))
        lf[node] = min((ls[s] for s in succs), default=project_finish)
        ls[node] = lf[node] - duration[node]

    records = []
    for node in order:
        row = df.loc[df["process"] == node].iloc[0]
        records.append({
            "process": node,
            "duration_raw": row["duration_raw"],
            "duration": duration[node],
            "earliest_start": es[node],
            "earliest_finish": ef[node],
            "latest_start": ls[node],
            "latest_finish": lf[node],
            "total_float": ls[node] - es[node],
            "critical": abs(ls[node] - es[node]) < 1e-9,
            "predecessors": sorted(list(g.predecessors(node))),
            "successors": sorted(list(g.successors(node))),
        })

    schedule_df = pd.DataFrame(records)
    dominant_path = compute_dominant_path(schedule_df)
    dominant_set = set(dominant_path)
    critical_edges = compute_dominant_edges(g, schedule_df, dominant_set)
    return {
        "lead_time": project_finish,
        "schedule": records,
        "dominant_path": dominant_path,
        "critical_edges": [{"from": u, "to": v} for u, v in critical_edges],
        "graph": {
            "nodes": [{"id": node, "duration": duration[node]} for node in order],
            "edges": [{"from": u, "to": v} for u, v in g.edges()],
        },
    }


HTML_PAGE = """<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Manufacturing Lead Time Calculator</title><style>
body{margin:0;font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a}.wrap{max-width:1600px;margin:0 auto;padding:24px}.grid-top{display:grid;grid-template-columns:1.6fr 1fr;gap:16px;margin-bottom:16px}.card{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:20px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}button,.file-label{border:1px solid #e2e8f0;background:#fff;border-radius:14px;padding:10px 14px;cursor:pointer}button.primary{background:#2563eb;color:#fff;border-color:#2563eb}.file-label input{display:none}.badge{display:inline-block;background:#eef2ff;color:#3730a3;padding:6px 10px;border-radius:999px;font-size:12px}.status-ok{border-radius:16px;padding:14px;margin-top:8px;background:#d1fae5;color:#065f46}.status-warn{border-radius:16px;padding:14px;margin-top:8px;background:#fef3c7;color:#92400e}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.stat{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:12px}.stat-label{color:#64748b;font-size:12px;text-transform:uppercase}.helper{color:#64748b;font-size:12px;margin-top:8px}th{text-align:left;color:#64748b;font-size:13px;padding:0 8px;white-space:nowrap}td{padding:0 8px}table{width:100%;border-collapse:separate;border-spacing:0 10px}input[type=text],input[type=number]{width:100%;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;font-size:14px}.section{margin-top:16px}.scroll{overflow-x:auto}.chart-box{overflow-x:auto;border:1px solid #e2e8f0;border-radius:16px;background:#fff;padding:10px}.head-input{min-width:120px;font-weight:600}@media (max-width:900px){.grid-top{grid-template-columns:1fr}.stats{grid-template-columns:1fr}}</style><script src='https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js'></script></head><body>
<div class='wrap'><div class='grid-top'><div class='card'><h1>Manufacturing Lead Time Calculator</h1><p>Duration may be numeric or a formula token such as <b>Lead_A</b>. Variable columns update dynamically from your formulas, manual edits, and uploaded Excel headers. You can add new variables and rename variable columns directly in the input table.</p><div class='actions'><button onclick='downloadTemplate()'>Download template</button><button onclick='downloadCurrentInput()'>Download current input</button><label class='file-label'>Upload Excel<input type='file' accept='.xlsx,.xls' onchange='uploadExcel(event)'></label><button onclick='addRow()'>Add row</button><button onclick='addVariable()'>Add variable</button><button class='primary' onclick='runCalculation()'>Run</button></div><div style='margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;'><span id='loadedFile' class='badge' style='display:none;'></span><span class='badge'>Sheets: PERT + FORMULA</span></div><div class='helper'>Editing formulas automatically exposes new variables as table columns. Renaming a variable header renames that field for every process row.</div></div><div class='card'><h2>Summary</h2><div id='summary'></div></div></div>
<div class='card section'><h2>Input table</h2><div class='scroll'><table><thead><tr id='inputHeaderRow'></tr></thead><tbody id='rowsBody'></tbody></table></div></div>
<div class='card section'><h2>Formula table</h2><div class='helper'>Use formulas like <b>(workload*workers)+set_up</b>. Variable names can be anything valid in Python formulas.</div><div class='scroll'><table><thead><tr><th>Name</th><th>Formula</th><th></th></tr></thead><tbody id='formulaBody'></tbody></table></div></div>
<div class='card section'><h2>Gantt chart</h2><div id='ganttContainer' class='chart-box'></div></div>
<div class='card section'><h2>Network diagram</h2><div id='networkContainer' class='chart-box'></div></div>
<div class='card section'><h2>Calculated schedule</h2><div class='scroll'><table id='scheduleTable'></table></div></div></div>
<script>
const sampleRows = %SAMPLE_ROWS_JSON%;
const sampleFormulas = %SAMPLE_FORMULAS_JSON%;
const BASE_KEYS = ['process','predecessor','successor','duration'];
let rows = structuredClone(sampleRows);
let formulas = structuredClone(sampleFormulas);
let variableColumns = [];
function esc(s){return String(s ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function formulaVariables(expr){const matches=String(expr||'').match(/[A-Za-z_]\w*/g)||[];const reserved=new Set(['abs','min','max','round','ceil','floor','sqrt']);return [...new Set(matches.filter(x=>!reserved.has(x)))];}
function normalizeVarName(name){return String(name||'').trim().replace(/\s+/g,'_').replace(/[^A-Za-z0-9_]/g,'_').replace(/^[^A-Za-z_]+/,'');}
function refreshVariableColumns(){const set=new Set(variableColumns);rows.forEach(r=>Object.keys(r).forEach(k=>{if(!BASE_KEYS.includes(k)&&String(k).trim())set.add(k);}));formulas.forEach(f=>formulaVariables(f.formula).forEach(v=>set.add(v)));variableColumns=[...set].filter(Boolean);rows=rows.map(r=>{const out={process:r.process||'',predecessor:r.predecessor||'NA',successor:r.successor||'STOP',duration:r.duration??''};variableColumns.forEach(v=>{out[v]=r[v]??'';});return out;});}
function renderHeader(){const head=document.getElementById('inputHeaderRow');head.innerHTML=`<th>Process</th><th>Predecessor</th><th>Successor</th><th>Duration / Token</th>`+variableColumns.map((v,i)=>`<th><input class='head-input' type='text' value='${esc(v)}' oninput='renameVariable(${i}, this.value)'></th>`).join('')+`<th></th>`;}
function renderRows(){refreshVariableColumns();renderHeader();const body=document.getElementById('rowsBody');body.innerHTML=rows.map((row,i)=>`<tr><td><input type='text' value='${esc(row.process)}' oninput="updateRow(${i},'process',this.value)"></td><td><input type='text' value='${esc(row.predecessor)}' oninput="updateRow(${i},'predecessor',this.value)"></td><td><input type='text' value='${esc(row.successor)}' oninput="updateRow(${i},'successor',this.value)"></td><td><input type='text' value='${esc(row.duration)}' oninput="updateRow(${i},'duration',this.value)"></td>`+variableColumns.map(v=>`<td><input type='number' step='any' value='${esc(row[v])}' oninput="updateVariableCell(${i},'${esc(v)}',this.value)"></td>`).join('')+`<td><button onclick='removeRow(${i})'>Delete</button></td></tr>`).join('');}
function renderFormulas(){const body=document.getElementById('formulaBody');body.innerHTML=formulas.map((row,i)=>`<tr><td><input type='text' value='${esc(row.name)}' oninput="updateFormula(${i},'name',this.value)"></td><td><input type='text' value='${esc(row.formula)}' oninput="updateFormula(${i},'formula',this.value)"></td><td><button onclick='removeFormula(${i})'>Delete</button></td></tr>`).join('')+`<tr><td colspan='3'><button onclick='addFormula()'>Add formula</button></td></tr>`;}
function updateRow(i,f,v){rows[i][f]=v;}
function updateVariableCell(i,f,v){rows[i][f]=v;}
function addRow(){const row={process:'',predecessor:'NA',successor:'STOP',duration:''};variableColumns.forEach(v=>row[v]='');rows.push(row);renderRows();}
function removeRow(i){rows.splice(i,1);if(!rows.length)addRow();else renderRows();}
function updateFormula(i,f,v){formulas[i][f]=v;refreshVariableColumns();renderRows();renderFormulas();}
function addFormula(){formulas.push({name:'',formula:''});renderFormulas();}
function removeFormula(i){formulas.splice(i,1);refreshVariableColumns();renderRows();renderFormulas();}
function addVariable(){const name=normalizeVarName(prompt('New variable name'));if(!name)return;if(BASE_KEYS.includes(name)){alert('That name is reserved.');return;}if(variableColumns.includes(name)){alert('Variable already exists.');return;}variableColumns.push(name);rows.forEach(r=>{r[name]='';});renderRows();}
function renameVariable(index,newNameRaw){const oldName=variableColumns[index];const newName=normalizeVarName(newNameRaw);if(!newName||BASE_KEYS.includes(newName))return;if(newName!==oldName&&variableColumns.includes(newName))return;variableColumns[index]=newName;rows=rows.map(r=>{const out={...r};out[newName]=out[oldName];if(newName!==oldName)delete out[oldName];return out;});renderRows();}
function rowsForApi(){return rows.filter(r=>r.process||r.predecessor||r.successor||r.duration!=='').map(r=>{const out={process:String(r.process||'').trim(),predecessor:String(r.predecessor||'NA').trim()||'NA',successor:String(r.successor||'STOP').trim()||'STOP',duration:String(r.duration??'').trim()};variableColumns.forEach(v=>{out[v]=r[v];});return out;});}
function formulasForApi(){return formulas.filter(r=>(r.name||'').trim()&&(r.formula||'').trim()).map(r=>({name:String(r.name).trim(),formula:String(r.formula).trim()}));}
function downloadTemplate(){window.open('/sample-excel','_blank');}
function downloadCurrentInput(){const headers=[...BASE_KEYS,...variableColumns];const wsPert=XLSX.utils.json_to_sheet(rows,{header:headers});const wsFormula=XLSX.utils.json_to_sheet(formulas,{header:['name','formula']});const wb=XLSX.utils.book_new();XLSX.utils.book_append_sheet(wb,wsPert,'PERT');XLSX.utils.book_append_sheet(wb,wsFormula,'FORMULA');XLSX.writeFile(wb,'PERT_input.xlsx');}
async function uploadExcel(event){const file=event.target.files?.[0];if(!file)return;const loaded=document.getElementById('loadedFile');loaded.style.display='inline-block';loaded.textContent='Loaded: '+file.name;const data=await file.arrayBuffer();const workbook=XLSX.read(data,{type:'array'});const pertSheet=workbook.Sheets.PERT||workbook.Sheets[workbook.SheetNames[0]];const rawPert=XLSX.utils.sheet_to_json(pertSheet,{header:1,defval:''});const hasHeader=(rawPert[0]||[]).slice(0,4).map(x=>String(x).trim().toLowerCase()).sort().join('|')===['process','predecessor','successor','duration'].sort().join('|');let headers=[],body=[];if(hasHeader){headers=(rawPert[0]||[]).map(x=>String(x).trim());body=rawPert.slice(1);}else{headers=[...BASE_KEYS];for(let i=4;i<(rawPert[0]||[]).length;i++)headers.push('p'+(i-4));body=rawPert;}variableColumns=headers.slice(4).map(normalizeVarName).filter(Boolean);rows=body.filter(r=>r.some(cell=>String(cell??'').trim()!=='' )).map(r=>{const row={process:String(r[0]??'').trim(),predecessor:String(r[1]??'NA').trim()||'NA',successor:String(r[2]??'STOP').trim()||'STOP',duration:String(r[3]??'').trim()};variableColumns.forEach((v,idx)=>{row[v]=String(r[idx+4]??'').trim();});return row;});if(!rows.length)rows=structuredClone(sampleRows);const formulaSheet=workbook.Sheets.FORMULA;if(formulaSheet){const rawFormula=XLSX.utils.sheet_to_json(formulaSheet,{header:1,defval:''});const formulaBody=rawFormula.slice(1);formulas=formulaBody.filter(r=>String(r[0]??'').trim()&&String(r[1]??'').trim()).map(r=>({name:String(r[0]??'').trim(),formula:String(r[1]??'').trim()}));}if(!formulas.length)formulas=structuredClone(sampleFormulas);refreshVariableColumns();renderRows();renderFormulas();}
async function runCalculation(){document.getElementById('summary').innerHTML='<div class="status-ok">Running calculation...</div>';try{const res=await fetch('/compute/json',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rows:rowsForApi(),formulas:formulasForApi()})});const data=await res.json();if(!res.ok){document.getElementById('summary').innerHTML=`<div class='status-warn'>${esc(data.detail||'Calculation failed.')}</div>`;return;}renderSummary(data);renderGantt(data);renderNetwork(data);renderScheduleTable(data);}catch{document.getElementById('summary').innerHTML='<div class="status-warn">Could not reach the application backend.</div>';}}
function renderSummary(data){const path=(data.dominant_path||[]).join(' → ')||'—';document.getElementById('summary').innerHTML=`<div class='status-ok'><strong>Calculation complete</strong><div class='stats'><div class='stat'><div class='stat-label'>Lead time</div><div class='stat-value'>${Math.round(data.lead_time)} min</div></div><div class='stat'><div class='stat-label'>Processes</div><div class='stat-value'>${(data.schedule||[]).length}</div></div></div><div class='stat' style='margin-top:10px;'><div class='stat-label'>Dominant critical path</div><div style='margin-top:6px;font-weight:600;'>${esc(path)}</div></div></div>`;}
function renderGantt(data){const schedule=(data.schedule||[]).slice().sort((a,b)=>a.earliest_start-b.earliest_start||a.process.localeCompare(b.process));const dominantSet=new Set(data.dominant_path||[]);const width=1000,rowHeight=36,labelWidth=110,rightPad=24,topPad=26,chartWidth=width-labelWidth-rightPad,height=topPad+schedule.length*rowHeight+30,finish=Math.max(data.lead_time||1,1),scale=value=>labelWidth+(value/finish)*chartWidth;let svg=`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`;const tickCount=Math.min(Math.round(finish)+1,21);for(let i=0;i<tickCount;i++){const tick=(finish/Math.max(tickCount-1,1))*i,x=scale(tick);svg+=`<line x1="${x}" y1="16" x2="${x}" y2="${height-16}" stroke="#e5e7eb" stroke-width="1" /><text x="${x}" y="12" text-anchor="middle" font-size="11" fill="#6b7280">${Math.round(tick)}</text>`;}schedule.forEach((row,idx)=>{const y=topPad+idx*rowHeight,x=scale(row.earliest_start),w=Math.max(scale(row.earliest_finish)-x,6),hue=(idx*37)%360,fill=`hsl(${hue} 70% 65%)`,isDominant=dominantSet.has(row.process),stroke=isDominant?'#dc2626':row.critical?'#111827':'transparent',strokeWidth=isDominant?3:row.critical?2:0;svg+=`<text x="10" y="${y+18}" font-size="12" fill="#111827">${esc(row.process)}</text><rect x="${x}" y="${y}" width="${w}" height="20" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="${strokeWidth}" /><text x="${x+6}" y="${y+14}" font-size="11" fill="#111827">${Math.round(row.duration)}</text>`;});svg+='</svg>';document.getElementById('ganttContainer').innerHTML=svg;}
function computeLevels(graph){const incoming={},out={};graph.nodes.forEach(n=>{incoming[n.id]=[];out[n.id]=[];});graph.edges.forEach(e=>{incoming[e.to].push(e.from);out[e.from].push(e.to);});const levels={},queue=graph.nodes.filter(n=>incoming[n.id].length===0).map(n=>n.id).sort();queue.forEach(id=>{levels[id]=0;});while(queue.length){const id=queue.shift();out[id].forEach(next=>{const proposed=(levels[id]||0)+1;levels[next]=Math.max(levels[next]||0,proposed);incoming[next]=incoming[next].filter(x=>x!==id);if(incoming[next].length===0)queue.push(next);});}return levels;}
function renderNetwork(data){const graph=data.graph||{nodes:[],edges:[]},dominantSet=new Set(data.dominant_path||[]),criticalEdgeSet=new Set((data.critical_edges||[]).map(e=>`${e.from}__${e.to}`)),levels=computeLevels(graph),groups={};graph.nodes.forEach(n=>{const lvl=levels[n.id]||0;if(!groups[lvl])groups[lvl]=[];groups[lvl].push(n.id);});Object.values(groups).forEach(arr=>arr.sort());const colWidth=170,rowGap=90,nodeW=110,nodeH=44,margin=30,maxLevel=Math.max(0,...Object.keys(groups).map(Number)),maxRows=Math.max(1,...Object.values(groups).map(a=>a.length)),width=margin*2+(maxLevel+1)*colWidth,height=Math.max(220,margin*2+maxRows*rowGap),pos={};Object.keys(groups).map(Number).sort((a,b)=>a-b).forEach(level=>{groups[level].forEach((id,idx)=>{pos[id]={x:margin+level*colWidth+20,y:margin+idx*rowGap+20};});});let svg=`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#6b7280" /></marker><marker id="arrowRed" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#dc2626" /></marker></defs>`;graph.edges.forEach(e=>{const from=pos[e.from],to=pos[e.to];if(!from||!to)return;const x1=from.x+nodeW,y1=from.y+nodeH/2,x2=to.x,y2=to.y+nodeH/2,mx=(x1+x2)/2,d=`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`,isDominant=criticalEdgeSet.has(`${e.from}__${e.to}`);svg+=`<path d="${d}" fill="none" stroke="${isDominant?'#dc2626':'#94a3b8'}" stroke-width="${isDominant?3:1.5}" marker-end="url(#${isDominant?'arrowRed':'arrow'})" />`;});graph.nodes.forEach((n,idx)=>{const p=pos[n.id];if(!p)return;const hue=(idx*37)%360,fill=`hsl(${hue} 70% 92%)`,isDominant=dominantSet.has(n.id);svg+=`<rect x="${p.x}" y="${p.y}" width="${nodeW}" height="${nodeH}" rx="12" fill="${fill}" stroke="${isDominant?'#dc2626':'#334155'}" stroke-width="${isDominant?3:1.5}" /><text x="${p.x+nodeW/2}" y="${p.y+nodeH/2+4}" text-anchor="middle" font-size="13" fill="#111827">${esc(n.id)}</text>`;});svg+='</svg>';document.getElementById('networkContainer').innerHTML=svg;}
function renderScheduleTable(data){const schedule=data.schedule||[],table=document.getElementById('scheduleTable');let html=`<thead><tr><th>Process</th><th>Duration token</th><th>Resolved duration</th><th>ES</th><th>EF</th><th>LS</th><th>LF</th><th>Float</th><th>Critical</th></tr></thead><tbody>`;schedule.forEach(r=>{html+=`<tr><td>${esc(r.process)}</td><td>${esc(r.duration_raw)}</td><td>${Math.round(r.duration)}</td><td>${Math.round(r.earliest_start)}</td><td>${Math.round(r.earliest_finish)}</td><td>${Math.round(r.latest_start)}</td><td>${Math.round(r.latest_finish)}</td><td>${Math.round(r.total_float)}</td><td>${r.critical?'Yes':'No'}</td></tr>`;});html+='</tbody>';table.innerHTML=html;}
refreshVariableColumns();renderRows();renderFormulas();document.getElementById('summary').innerHTML='<div class="status-warn">Press Run to calculate.</div>';
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return HTML_PAGE.replace("%SAMPLE_ROWS_JSON%", json.dumps(SAMPLE_ROWS)).replace("%SAMPLE_FORMULAS_JSON%", json.dumps(SAMPLE_FORMULAS))


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/sample-excel")
def sample_excel() -> StreamingResponse:
    pert_df = pd.DataFrame(SAMPLE_ROWS)
    formula_df = pd.DataFrame(SAMPLE_FORMULAS)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pert_df.to_excel(writer, sheet_name=SHEET_NAME, index=False)
        formula_df.to_excel(writer, sheet_name=FORMULA_SHEET_NAME, index=False)
    output.seek(0)
    headers = {"Content-Disposition": 'attachment; filename="PERT_sample.xlsx"'}
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)


@app.post("/compute/json")
def compute_from_json(payload: ComputeRequest) -> JSONResponse:
    try:
        formula_map = build_formula_map(pd.DataFrame([row.model_dump() for row in (payload.formulas or [])]))
        df = dataframe_from_rows([row.model_dump() for row in payload.rows], formula_map)
        return JSONResponse(compute_schedule(df))
    except (PertDataError, CycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc


@app.post("/compute/excel")
async def compute_from_excel(file: UploadFile = File(...)) -> JSONResponse:
    try:
        content = await file.read()
        df, _formula_map = load_pert_excel_from_bytes(content)
        return JSONResponse(compute_schedule(df))
    except (PertDataError, CycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
