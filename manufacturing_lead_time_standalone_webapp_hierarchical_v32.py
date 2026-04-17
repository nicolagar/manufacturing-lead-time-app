
from io import BytesIO
import math
import ast, json, math, re
from typing import Dict, List, Set, Tuple, Optional

import networkx as nx
import pandas as pd
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

SHEET_NAME = "PERT"
FORMULA_SHEET_NAME = "FORMULA"

SAMPLE_ROWS = [
    {"process":"TOP_A","refines":"","predecessor":"NA","successor":"TOP_B","duration":"INHERITED","workload":"","workers":"","set_up":""},
    {"process":"TOP_B","refines":"","predecessor":"TOP_A","successor":"STOP","duration":"LT_A","workload":100,"workers":20,"set_up":10},
    {"process":"A1","refines":"TOP_A","predecessor":"NA","successor":"A2;A3","duration":50},
    {"process":"A2","refines":"TOP_A","predecessor":"A1","successor":"A4","duration":"LT_B","workers":5},
    {"process":"A3","refines":"TOP_A","predecessor":"A1","successor":"A4","duration":30},
    {"process":"A4","refines":"TOP_A","predecessor":"A2;A3","successor":"STOP","duration":20},
]
SAMPLE_FORMULAS = [
    {"name":"LT_A","formula":"(workload*workers)+set_up"},
    {"name":"LT_B","formula":"workers*workers"},
]

class PertDataError(Exception): pass
class CycleError(Exception): pass

class FormulaRow(BaseModel):
    name: str
    formula: str

class ComputeRequest(BaseModel):
    rows: List[Dict]
    formulas: List[FormulaRow] | None = None

app = FastAPI(title="Manufacturing Lead Time Web App")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def normalize_text(value) -> str:
    if pd.isna(value): return ""
    return str(value).strip()

def split_logic_field(value: str, terminal_token: str) -> List[str]:
    text = normalize_text(value)
    if not text or text.upper() == terminal_token: return []
    return [item.strip() for item in text.split(";") if item.strip()]

def coerce_number(value):
    text = normalize_text(value)
    if text == "": return None
    try: return float(text)
    except ValueError: return None


def is_inherited_marker(value) -> bool:
    return normalize_text(value).upper() == "INHERITED"


def ensure_no_nan(value, path="result"):
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise PertDataError(f"Invalid numeric value generated at {path}.")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            ensure_no_nan(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            ensure_no_nan(v, f"{path}[{i}]")

def build_formula_map(formula_df: pd.DataFrame) -> Dict[str, str]:
    if formula_df.empty: return {}
    first_two = formula_df.iloc[:, :2].copy()
    first_two.columns = ["name","formula"]
    first_two["name"] = first_two["name"].map(normalize_text)
    first_two["formula"] = first_two["formula"].map(normalize_text)
    first_two = first_two[(first_two["name"] != "") & (first_two["formula"] != "")]
    return dict(zip(first_two["name"], first_two["formula"]))

def extract_formula_variables(expression: str) -> List[str]:
    tree = ast.parse(expression, mode="eval")
    funcs = {"abs","min","max","round","ceil","floor","sqrt","log","ln","log10","exp","sin","cos","tan","asin","acos","atan","atan2","sinh","cosh","tanh","pi","e"}
    return sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in funcs})

def safe_eval_formula(expression: str, variables: Dict[str, float]) -> float:
    funcs = {
        "abs": abs, "min": min, "max": max, "round": round,
        "ceil": math.ceil, "floor": math.floor, "sqrt": math.sqrt,
        "log": math.log, "ln": math.log, "log10": math.log10, "exp": math.exp,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
        "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
        "pi": math.pi, "e": math.e,
    }
    allowed = (
        ast.Expression, ast.BinOp, ast.UnaryOp, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod,
        ast.USub, ast.UAdd, ast.Load, ast.Name, ast.Constant, ast.Call, ast.FloorDiv,
        ast.IfExp, ast.Compare, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
        ast.BoolOp, ast.And, ast.Or, ast.Not
    )
    tree = ast.parse(expression, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, allowed):
            raise PertDataError(f"Unsupported formula syntax: {expression}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in funcs:
                raise PertDataError(f"Unsupported function in formula: {expression}")
        if isinstance(node, ast.Name) and node.id not in variables and node.id not in funcs:
            raise PertDataError(f"Missing variable '{node.id}' for formula: {expression}")
    result = eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, {**funcs, **variables})
    if isinstance(result, bool):
        return float(result)
    result = float(result)
    if math.isnan(result) or math.isinf(result):
        raise PertDataError(f"Formula produced invalid numeric value: {expression}")
    return result

def resolve_numeric_or_formula_duration(row: pd.Series, formula_map: Dict[str, str], extra_columns: List[str]) -> Optional[float]:
    raw_duration = row["duration_raw"]
    numeric_duration = coerce_number(raw_duration)
    if numeric_duration is not None:
        return numeric_duration
    token = normalize_text(raw_duration)
    if is_inherited_marker(token):
        return None
    if token == "":
        raise PertDataError(
            f"Process '{row['process']}' has an empty duration. Use a number, a formula token, or 'INHERITED'."
        )
    if token not in formula_map:
        raise PertDataError(f"Duration token '{token}' for process '{row['process']}' was not found in sheet '{FORMULA_SHEET_NAME}'.")
    expression = formula_map[token]
    needed_vars = extract_formula_variables(expression)
    header_vars = {}
    for col in extra_columns:
        name = normalize_text(col)
        if re.fullmatch(r"[A-Za-z_]\w*", name):
            header_vars[name] = coerce_number(row.get(col))
    positional_vars = {f"p{i}": coerce_number(row.get(col)) for i, col in enumerate(extra_columns)}
    variables, missing = {}, []
    for name in needed_vars:
        value = header_vars.get(name)
        if value is None: value = positional_vars.get(name)
        if value is None: missing.append(name)
        else: variables[name] = value
    if missing:
        raise PertDataError(f"Missing parameter values {missing} for process '{row['process']}' using formula '{token}'.")
    return safe_eval_formula(expression, variables)

def validate_references(df: pd.DataFrame) -> None:
    process_set = set(df["process"])
    if df["process"].eq("").any():
        bad = (df.index[df["process"].eq("")] + 1).tolist()
        raise PertDataError(f"Empty process name found in rows: {bad}")
    if df["process"].duplicated().any():
        dup = df.loc[df["process"].duplicated(keep=False), "process"].tolist()
        raise PertDataError(f"Duplicate process names found: {sorted(set(dup))}")
    invalid_refines = sorted({r for r in df["refines"] if normalize_text(r) and normalize_text(r) not in process_set})
    if invalid_refines:
        raise PertDataError(f"Unknown refines references: {invalid_refines}")
    children_by_parent = df.groupby("refines")["process"].apply(list).to_dict() if not df.empty else {}
    bad_pred, bad_succ = set(), set()
    for _, row in df.iterrows():
        parent = normalize_text(row["refines"])
        siblings = set(children_by_parent.get(parent, []))
        siblings.add(row["process"])
        for pred in split_logic_field(row["predecessor"], "NA"):
            if pred not in siblings: bad_pred.add(f"{row['process']} -> {pred}")
        for succ in split_logic_field(row["successor"], "STOP"):
            if succ not in siblings: bad_succ.add(f"{row['process']} -> {succ}")
    if bad_pred:
        raise PertDataError(f"Predecessor references must stay within the same refines level: {sorted(bad_pred)}")
    if bad_succ:
        raise PertDataError(f"Successor references must stay within the same refines level: {sorted(bad_succ)}")

def prepare_dataframe(df: pd.DataFrame, formula_map: Dict[str, str]) -> pd.DataFrame:
    required = ["process","refines","predecessor","successor","duration_raw"]
    for col in required:
        if col not in df.columns: raise PertDataError(f"Missing required column: {col}")
    for col in ["process","refines","predecessor","successor"]:
        df[col] = df[col].map(normalize_text)
    validate_references(df)
    extra_columns = [col for col in df.columns if col not in required]
    df["base_duration"] = df.apply(lambda row: resolve_numeric_or_formula_duration(row, formula_map, extra_columns), axis=1)
    return df

def dataframe_from_rows(rows: List[Dict], formula_map: Dict[str, str] | None = None) -> pd.DataFrame:
    formula_map = formula_map or {}
    df = pd.DataFrame(rows)
    if df.empty: raise PertDataError("No process rows provided.")
    if "refines" not in df.columns: df["refines"] = ""
    required = ["process","refines","predecessor","successor","duration"]
    missing = [c for c in required if c not in df.columns]
    if missing: raise PertDataError(f"Missing fields: {missing}")
    return prepare_dataframe(df.rename(columns={"duration":"duration_raw"}), formula_map)

def load_pert_excel_from_bytes(content: bytes) -> Tuple[pd.DataFrame, Dict[str, str]]:
    buffer = BytesIO(content)
    preview = pd.read_excel(buffer, sheet_name=SHEET_NAME, header=None)
    if preview.shape[1] < 5:
        raise PertDataError(f"Sheet '{SHEET_NAME}' must contain at least 5 columns: process, refines, predecessor, successor, duration.")

    normalized_first_row = [normalize_text(x).lower() for x in preview.iloc[0].tolist()]
    required_headers = {"process", "refines", "predecessor", "successor", "duration"}

    has_header = required_headers.issubset(set(normalized_first_row))

    if has_header:
        buffer.seek(0)
        raw_df = pd.read_excel(buffer, sheet_name=SHEET_NAME).copy()

        header_lookup = {}
        for idx, col in enumerate(raw_df.columns):
            key = normalize_text(col).lower()
            if key and key not in header_lookup:
                header_lookup[key] = idx

        missing = [h for h in ["process", "refines", "predecessor", "successor", "duration"] if h not in header_lookup]
        if missing:
            raise PertDataError(f"Missing required headers in sheet '{SHEET_NAME}': {missing}")

        ordered_indices = [
            header_lookup["process"],
            header_lookup["refines"],
            header_lookup["predecessor"],
            header_lookup["successor"],
            header_lookup["duration"],
        ]
        extra_indices = [i for i in range(len(raw_df.columns)) if i not in ordered_indices]

        selected = raw_df.iloc[:, ordered_indices + extra_indices].copy()
        extra_names = []
        for pos, original_idx in enumerate(extra_indices):
            original_name = normalize_text(raw_df.columns[original_idx])
            extra_names.append(original_name or f"p{pos}")

        selected.columns = ["process", "refines", "predecessor", "successor", "duration_raw"] + extra_names
        df = selected
    else:
        extra_count = max(0, preview.shape[1] - 5)
        df = preview.copy()
        df.columns = ["process", "refines", "predecessor", "successor", "duration_raw"] + [f"p{i}" for i in range(extra_count)]

    formula_df = pd.read_excel(BytesIO(content), sheet_name=FORMULA_SHEET_NAME, header=None)
    formula_map = build_formula_map(formula_df)
    return prepare_dataframe(df, formula_map), formula_map

def build_level_graph(level_df: pd.DataFrame) -> nx.DiGraph:
    g = nx.DiGraph()
    for _, row in level_df.iterrows():
        g.add_node(row["process"], duration=float(row["duration"]))
    for _, row in level_df.iterrows():
        for pred in split_logic_field(row["predecessor"], "NA"): g.add_edge(pred, row["process"])
        for succ in split_logic_field(row["successor"], "STOP"): g.add_edge(row["process"], succ)
    if not nx.is_directed_acyclic_graph(g):
        raise CycleError("The process network contains a cycle. Critical path calculation requires a DAG.")
    return g

def compute_level_schedule(level_df: pd.DataFrame, offset: float) -> Tuple[pd.DataFrame, float, nx.DiGraph]:
    g = build_level_graph(level_df)
    duration = nx.get_node_attributes(g, "duration")
    order = list(nx.topological_sort(g))
    indeg = {n: g.in_degree(n) for n in g.nodes}
    outdeg = {n: g.out_degree(n) for n in g.nodes}

    def check_value(value: float, process: str, field: str) -> None:
        if math.isnan(value) or math.isinf(value):
            raise PertDataError(f"Invalid numeric value for process '{process}' at field '{field}'.")

    es, ef = {}, {}

    for n in order:
        dur = float(duration[n])
        check_value(dur, n, "duration")
        preds = list(g.predecessors(n))
        es[n] = max((ef[p] for p in preds), default=0.0)
        ef[n] = es[n] + dur
        check_value(es[n], n, "earliest_start_forward_1")
        check_value(ef[n], n, "earliest_finish_forward_1")

    for n in order:
        if list(g.predecessors(n)):
            continue
        succs = [s for s in g.successors(n) if indeg[s] > 1]
        feasible = [es[s] - duration[n] for s in succs if es[s] - duration[n] >= 0]
        if feasible:
            es[n] = min(feasible)
            ef[n] = es[n] + duration[n]
            check_value(es[n], n, "earliest_start_source_adjusted")
            check_value(ef[n], n, "earliest_finish_source_adjusted")

    for n in order:
        preds = list(g.predecessors(n))
        if preds:
            candidate = max(ef[p] for p in preds)
            check_value(candidate, n, "earliest_start_forward_2_candidate")
            es[n] = candidate
        ef[n] = es[n] + duration[n]
        check_value(es[n], n, "earliest_start_forward_2")
        check_value(ef[n], n, "earliest_finish_forward_2")

    for n in order:
        succs = list(g.successors(n))
        preds = list(g.predecessors(n))
        if succs or len(preds) != 1:
            continue
        pred = preds[0]
        if outdeg[pred] > 1:
            es[n] = max(es[n], ef[pred])
            ef[n] = es[n] + duration[n]
            check_value(es[n], n, "earliest_start_sink_adjusted")
            check_value(ef[n], n, "earliest_finish_sink_adjusted")

    for n in order:
        preds = list(g.predecessors(n))
        if preds:
            candidate = max(ef[p] for p in preds)
            check_value(candidate, n, "earliest_start_forward_3_candidate")
            es[n] = candidate
        ef[n] = es[n] + duration[n]
        check_value(es[n], n, "earliest_start_forward_3")
        check_value(ef[n], n, "earliest_finish_forward_3")

    lead = max(ef.values()) if ef else 0.0
    if math.isnan(lead) or math.isinf(lead):
        raise PertDataError("Level schedule produced an invalid lead time.")

    ls, lf = {}, {}
    for n in reversed(order):
        succs = list(g.successors(n))
        if succs:
            missing_succ = [s for s in succs if s not in ls]
            if missing_succ:
                raise PertDataError(
                    f"Backward-pass error for process '{n}': latest start missing for successors {missing_succ}."
                )
            candidate_lf = min(ls[s] for s in succs)
        else:
            candidate_lf = lead
        check_value(candidate_lf, n, "latest_finish_candidate")
        lf[n] = candidate_lf
        ls[n] = lf[n] - duration[n]
        check_value(lf[n], n, "latest_finish")
        check_value(ls[n], n, "latest_start")

    recs = []
    for n in order:
        rec = {
            "process": n,
            "earliest_start": es[n] + offset,
            "earliest_finish": ef[n] + offset,
            "latest_start": ls[n] + offset,
            "latest_finish": lf[n] + offset,
            "total_float": ls[n] - es[n],
            "critical": abs(ls[n] - es[n]) < 1e-9,
            "predecessors": sorted(g.predecessors(n)),
            "successors": sorted(g.successors(n)),
        }
        check_value(rec["earliest_start"], n, "earliest_start_offset")
        check_value(rec["earliest_finish"], n, "earliest_finish_offset")
        check_value(rec["latest_start"], n, "latest_start_offset")
        check_value(rec["latest_finish"], n, "latest_finish_offset")
        check_value(rec["total_float"], n, "total_float")
        recs.append(rec)

    return pd.DataFrame(recs), lead, g

def compute_dominant_path(schedule_df: pd.DataFrame) -> List[str]:
    if schedule_df.empty: return []
    index = schedule_df.set_index("process")
    project_finish = float(schedule_df["earliest_finish"].max())
    sinks = sorted(schedule_df.loc[schedule_df["earliest_finish"] == project_finish, "process"].tolist())
    current = sinks[0] if sinks else None
    path = []
    while current:
        path.insert(0, current)
        row = index.loc[current]
        cands = []
        for pred in row["predecessors"]:
            if pred in index.index:
                prow = index.loc[pred]
                if prow["critical"] and abs(prow["earliest_finish"] - row["earliest_start"]) < 1e-9:
                    cands.append((pred, prow["earliest_finish"]))
        cands.sort(key=lambda x: x[1], reverse=True)
        current = cands[0][0] if cands else None
    return path

def compute_dominant_edges(g: nx.DiGraph, schedule_df: pd.DataFrame, dominant_set: Set[str]) -> Set[Tuple[str, str]]:
    if schedule_df.empty: return set()
    index = schedule_df.set_index("process")
    out = set()
    for u, v in g.edges():
        if u in dominant_set and v in dominant_set and abs(index.loc[u, "earliest_finish"] - index.loc[v, "earliest_start"]) < 1e-9:
            out.add((u, v))
    return out

def compute_hierarchical_schedule(df: pd.DataFrame) -> Dict:
    df = df.copy()
    child_map = df.groupby("refines")["process"].apply(list).to_dict()
    resolved_duration: Dict[str, float] = {}
    records, graph_nodes, graph_edges = [], [], []

    def resolve_node_duration(process: str, stack: Optional[Set[str]] = None) -> float:
        if process in resolved_duration: return resolved_duration[process]
        stack = stack or set()
        if process in stack: raise CycleError(f"Cycle detected in refines hierarchy at process '{process}'.")
        stack.add(process)
        row = df.loc[df["process"] == process].iloc[0]
        base = row["base_duration"]
        children = child_map.get(process, [])
        base_is_valid = base is not None and not pd.isna(base)
        if base_is_valid:
            dur = float(base)
            if math.isnan(dur) or math.isinf(dur):
                raise PertDataError(
                    f"Process '{process}' has an invalid direct duration value."
                )
        elif children:
            child_df = df[df["process"].isin(children)].copy()
            for child in children:
                child_df.loc[child_df["process"] == child, "duration"] = resolve_node_duration(child, stack)
            _, child_lead, _ = compute_level_schedule(child_df, 0.0)
            dur = float(child_lead)
            if math.isnan(dur) or math.isinf(dur):
                raise PertDataError(f"Process '{process}' has empty duration and its child network produced an invalid lead time.")
        else:
            raise PertDataError(f"Process '{process}' is marked INHERITED but has no sub-level processes refining it.")
        resolved_duration[process] = dur
        stack.remove(process)
        return dur

    for process in df["process"].tolist():
        resolve_node_duration(process)

    df["duration"] = df["process"].map(resolved_duration)

    def schedule_level(parent: str, offset: float, depth: int):
        level_df = df[df["refines"] == parent].copy()
        if level_df.empty: return
        sched_df, _, g = compute_level_schedule(level_df, offset)
        for _, srow in sched_df.iterrows():
            proc = srow["process"]
            src = df.loc[df["process"] == proc].iloc[0]
            records.append({"process":proc,"refines":src["refines"],"depth":depth,"duration_raw":src["duration_raw"],"duration":float(src["duration"]),"earliest_start":float(srow["earliest_start"]),"earliest_finish":float(srow["earliest_finish"]),"latest_start":float(srow["latest_start"]),"latest_finish":float(srow["latest_finish"]),"total_float":float(srow["total_float"]),"critical":bool(srow["critical"]),"predecessors":srow["predecessors"],"successors":srow["successors"]})
            graph_nodes.append({"id":proc,"duration":float(src["duration"]),"depth":depth,"refines":src["refines"]})
        for u, v in g.edges():
            graph_edges.append({"from":u,"to":v,"level_parent":parent})
        for _, srow in sched_df.iterrows():
            proc = srow["process"]
            if proc in child_map: schedule_level(proc, float(srow["earliest_start"]), depth + 1)

    schedule_level("", 0.0, 0)
    schedule_df = pd.DataFrame(records).drop_duplicates(subset=["process"], keep="first")
    top_df = schedule_df[schedule_df["depth"] == 0].copy()
    project_finish = float(top_df["earliest_finish"].max()) if not top_df.empty else 0.0
    dominant_path = compute_dominant_path(top_df)
    dominant_set = set(dominant_path)
    top_graph = nx.DiGraph()
    for _, row in top_df.iterrows(): top_graph.add_node(row["process"], duration=row["duration"])
    for edge in graph_edges:
        if edge["level_parent"] == "": top_graph.add_edge(edge["from"], edge["to"])
    critical_edges = compute_dominant_edges(top_graph, top_df, dominant_set)
    result = {"lead_time":project_finish,"schedule":schedule_df.sort_values(["depth","earliest_start","process"]).to_dict(orient="records"),"dominant_path":dominant_path,"critical_edges":[{"from":u,"to":v} for u,v in critical_edges],"graph":{"nodes":graph_nodes,"edges":graph_edges}}
    ensure_no_nan(result)
    return result

HTML_PAGE = """<!DOCTYPE html><html><head><meta charset='UTF-8'><meta name='viewport' content='width=device-width, initial-scale=1.0'><title>Manufacturing Lead Time Calculator</title><style>body{margin:0;font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a}.wrap{max-width:1520px;margin:0 auto;padding:24px}.grid-top{display:grid;grid-template-columns:1.6fr 1fr;gap:16px;margin-bottom:16px}.card{background:#fff;border:1px solid #e2e8f0;border-radius:20px;padding:20px}.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}button,.file-label{border:1px solid #e2e8f0;background:#fff;border-radius:14px;padding:10px 14px;cursor:pointer}button.primary{background:#2563eb;color:#fff;border-color:#2563eb}.file-label input{display:none}.badge{display:inline-block;background:#eef2ff;color:#3730a3;padding:6px 10px;border-radius:999px;font-size:12px}.status-ok{border-radius:16px;padding:14px;margin-top:8px;background:#d1fae5;color:#065f46}.status-warn{border-radius:16px;padding:14px;margin-top:8px;background:#fef3c7;color:#92400e}.stats{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.stat{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:12px}.stat-label{color:#64748b;font-size:12px;text-transform:uppercase}table{width:100%;border-collapse:separate;border-spacing:0 10px}th{text-align:left;color:#64748b;font-size:13px;padding:0 8px;white-space:nowrap}td{padding:0 8px}input[type=text],input[type=number]{width:100%;border:1px solid #e2e8f0;border-radius:12px;padding:10px 12px;font-size:14px}.section{margin-top:16px}.scroll{overflow-x:auto}.chart-box{overflow-x:auto;border:1px solid #e2e8f0;border-radius:16px;background:#fff;padding:10px}.small{font-size:12px;color:#64748b}@media (max-width:900px){.grid-top{grid-template-columns:1fr}.stats{grid-template-columns:1fr}}</style><script src='https://cdn.jsdelivr.net/npm/xlsx@0.18.5/dist/xlsx.full.min.js'></script></head><body><div class='wrap'><div class='grid-top'><div class='card'><h1>Manufacturing Lead Time Calculator by N.Gargano</h1><p>Supports hierarchical decomposition with <b>refines</b>. Use <b>INHERITED</b> in duration when the process duration must come from its child network lead time.</p><div class='actions'><button onclick='downloadTemplate()'>Download template</button><button onclick='downloadCurrentInput()'>Download current input</button><label class='file-label'>Upload Excel<input type='file' accept='.xlsx,.xls' onchange='uploadExcel(event)'></label><button onclick='addRow()'>Add row</button><button onclick='addVariable()'>Add variable</button><button class='primary' onclick='runCalculation()'>Run</button></div><div style='margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;'><span id='loadedFile' class='badge' style='display:none;'></span><span class='badge'>Sheets: PERT + FORMULA</span></div><div style='font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;word-break:break-all;'></div></div></div><div class='card'><h2>Summary</h2><div id='summary'></div></div></div><div class='card section'><h2>Input table</h2><p class='small'>Use refines to point to the parent process. Use duration = <b>INHERITED</b> when a parent process must roll up the lead time of its child network. Predecessor and successor references must stay within the same refines level.</p><div class='scroll'><table><thead><tr id='headerRow'></tr></thead><tbody id='rowsBody'></tbody></table></div></div><div class='card section'><h2>Formula table</h2><div class='scroll'><table><thead><tr><th>Name</th><th>Formula</th><th></th></tr></thead><tbody id='formulaBody'></tbody></table></div></div><div class='card section'><h2>Gantt chart</h2><div class='small' style='margin-bottom:8px'>Each bar shows start time (S), duration (D), and end time (E).</div><div id='ganttContainer' class='chart-box'></div></div><div class='card section'><h2>Network diagram</h2><div class='small' style='margin-bottom:8px'></div><div id='networkContainer' class='chart-box'></div></div><div class='card section'><h2>Calculated schedule</h2><div class='scroll'><table id='scheduleTable'></table></div></div></div><script>
const sampleRows=%SAMPLE_ROWS_JSON%;const sampleFormulas=%SAMPLE_FORMULAS_JSON%;let rows=structuredClone(sampleRows);let formulas=structuredClone(sampleFormulas);let variableColumns=[];let expandedParents={};
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function extractFormulaVariables(expr){const names=(String(expr||'').match(/[A-Za-z_]\\w*/g)||[]);const funcs=new Set(['abs','min','max','round','ceil','floor','sqrt','log','ln','log10','exp','sin','cos','tan','asin','acos','atan','atan2','sinh','cosh','tanh','pi','e','if','and','or','not']);return [...new Set(names.filter(n=>!funcs.has(n)))];}
function syncVariableColumns(){const fixed=['process','refines','predecessor','successor','duration'];const fromRows=Object.keys(rows.reduce((a,r)=>Object.assign(a,r),{})).filter(k=>!fixed.includes(k));const fromFormulas=[...new Set(formulas.flatMap(f=>extractFormulaVariables(f.formula)))];variableColumns=[...new Set([...variableColumns,...fromRows,...fromFormulas])].filter(v=>v&&/^[A-Za-z_]\\w*$/.test(v));rows=rows.map(r=>{const o={process:r.process||'',refines:r.refines||'',predecessor:r.predecessor||'NA',successor:r.successor||'STOP',duration:r.duration??''};variableColumns.forEach(v=>o[v]=r[v]??'');return o;});}
function renameVariable(oldName,newName){newName=String(newName||'').trim();if(!newName||oldName===newName||variableColumns.includes(newName))return;variableColumns=variableColumns.map(v=>v===oldName?newName:v);rows=rows.map(r=>{r[newName]=r[oldName]??'';delete r[oldName];return r;});formulas=formulas.map(f=>({name:f.name,formula:String(f.formula||'').replace(new RegExp(`\\\\b${oldName}\\\\b`,'g'),newName)}));renderAll();}
function addVariable(){const base='var';let i=1,name=`${base}${i}`;while(variableColumns.includes(name)){i++;name=`${base}${i}`;}variableColumns.push(name);rows=rows.map(r=>({...r,[name]:''}));renderAll();}
function renderHeader(){const h=document.getElementById('headerRow');let html='<th>Process</th><th>Refines</th><th>Predecessor</th><th>Successor</th><th>Duration / Token</th>';variableColumns.forEach(v=>{html+=`<th><input type='text' value='${esc(v)}' onblur="renameVariable('${esc(v)}', this.value)" /></th>`;});html+='<th></th>';h.innerHTML=html;}
function renderRows(){const body=document.getElementById('rowsBody');body.innerHTML=rows.map((row,i)=>`<tr><td><input type='text' value='${esc(row.process)}' oninput="updateRow(${i},'process',this.value)"></td><td><input type='text' value='${esc(row.refines)}' oninput="updateRow(${i},'refines',this.value)"></td><td><input type='text' value='${esc(row.predecessor)}' oninput="updateRow(${i},'predecessor',this.value)"></td><td><input type='text' value='${esc(row.successor)}' oninput="updateRow(${i},'successor',this.value)"></td><td><input type='text' value='${esc(row.duration)}' oninput="updateRow(${i},'duration',this.value)"></td>${variableColumns.map(v=>`<td><input type='text' value='${esc(row[v])}' oninput="updateRow(${i},'${v}',this.value)"></td>`).join('')}<td><button onclick='removeRow(${i})'>Delete</button></td></tr>`).join('');}
function renderFormulas(){const body=document.getElementById('formulaBody');body.innerHTML=formulas.map((row,i)=>`<tr><td><input type='text' value='${esc(row.name)}' oninput="updateFormula(${i},'name',this.value)"></td><td><input type='text' value='${esc(row.formula)}' oninput="updateFormula(${i},'formula',this.value)"></td><td><button onclick='removeFormula(${i})'>Delete</button></td></tr>`).join('')+`<tr><td colspan='3'><button onclick='addFormula()'>Add formula</button></td></tr>`;}
function renderAll(){syncVariableColumns();renderHeader();renderRows();renderFormulas();}
function updateRow(i,f,v){rows[i][f]=v;}function addRow(){const row={process:'',refines:'',predecessor:'NA',successor:'STOP',duration:''};variableColumns.forEach(v=>row[v]='');rows.push(row);renderRows();}function removeRow(i){rows.splice(i,1);if(!rows.length)addRow();else renderRows();}function updateFormula(i,f,v){formulas[i][f]=v;syncVariableColumns();renderAll();}function addFormula(){formulas.push({name:'',formula:''});renderAll();}function removeFormula(i){formulas.splice(i,1);renderAll();}
function rowsForApi(){return rows.filter(r=>r.process||r.refines||r.predecessor||r.successor||r.duration!=='').map(r=>{const o={process:String(r.process||'').trim(),refines:String(r.refines||'').trim(),predecessor:String(r.predecessor||'NA').trim()||'NA',successor:String(r.successor||'STOP').trim()||'STOP',duration:String(r.duration??'').trim()};variableColumns.forEach(v=>o[v]=r[v]);return o;});}
function formulasForApi(){return formulas.filter(r=>(r.name||'').trim()&&(r.formula||'').trim()).map(r=>({name:String(r.name).trim(),formula:String(r.formula).trim()}));}
function downloadTemplate(){window.open('/sample-excel','_blank');}
function downloadCurrentInput(){const wsPert=XLSX.utils.json_to_sheet(rows,{header:['process','refines','predecessor','successor','duration',...variableColumns]});const wsFormula=XLSX.utils.json_to_sheet(formulas,{header:['name','formula']});const wb=XLSX.utils.book_new();XLSX.utils.book_append_sheet(wb,wsPert,'PERT');XLSX.utils.book_append_sheet(wb,wsFormula,'FORMULA');XLSX.writeFile(wb,'PERT_input.xlsx');}
async function uploadExcel(event){const file=event.target.files?.[0];if(!file)return;const loaded=document.getElementById('loadedFile');loaded.style.display='inline-block';loaded.textContent='Loaded: '+file.name;const data=await file.arrayBuffer();const workbook=XLSX.read(data,{type:'array'});const pertSheet=workbook.Sheets.PERT||workbook.Sheets[workbook.SheetNames[0]];const rawPert=XLSX.utils.sheet_to_json(pertSheet,{header:1,defval:''});const normalizedHeader=(rawPert[0]||[]).map(x=>String(x).trim().toLowerCase());const required=['process','refines','predecessor','successor','duration'];const hasHeader=required.every(h=>normalizedHeader.includes(h));let body=rawPert;let extraHeaders=[];if(hasHeader){const headerLookup={};normalizedHeader.forEach((name,idx)=>{if(name && !(name in headerLookup))headerLookup[name]=idx;});const orderedIdx=[headerLookup['process'],headerLookup['refines'],headerLookup['predecessor'],headerLookup['successor'],headerLookup['duration']];const extraIdx=(rawPert[0]||[]).map((_,idx)=>idx).filter(idx=>!orderedIdx.includes(idx));extraHeaders=extraIdx.map(idx=>String(rawPert[0][idx]??'').trim()).filter(Boolean);variableColumns=[...new Set(extraHeaders)];body=rawPert.slice(1).map(r=>{const ordered=[orderedIdx[0],orderedIdx[1],orderedIdx[2],orderedIdx[3],orderedIdx[4],...extraIdx].map(idx=>r[idx]);return ordered;});}else{variableColumns=[...(rawPert[0]||[]).slice(5).map(x=>String(x).trim()).filter(Boolean)];}rows=body.filter(r=>r.some(cell=>String(cell??'').trim()!=='' )).map(r=>{const obj={process:String(r[0]??'').trim(),refines:String(r[1]??'').trim(),predecessor:String(r[2]??'NA').trim()||'NA',successor:String(r[3]??'STOP').trim()||'STOP',duration:String(r[4]??'').trim()};variableColumns.forEach((v,idx)=>obj[v]=String(r[idx+5]??'').trim());return obj;});if(!rows.length)rows=structuredClone(sampleRows);const formulaSheet=workbook.Sheets.FORMULA;if(formulaSheet){const rawFormula=XLSX.utils.sheet_to_json(formulaSheet,{header:1,defval:''});formulas=rawFormula.slice(1).filter(r=>String(r[0]??'').trim()&&String(r[1]??'').trim()).map(r=>({name:String(r[0]??'').trim(),formula:String(r[1]??'').trim()}));}if(!formulas.length)formulas=structuredClone(sampleFormulas);renderAll();}
async function runCalculation(){
  document.getElementById('summary').innerHTML='<div class="status-ok">Running calculation...</div>';
  const payload={rows:rowsForApi(),formulas:formulasForApi()};
  let res;
  try{
    res=await fetch('/compute/json',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)
    });
  }catch(err){
    document.getElementById('summary').innerHTML=`<div class="status-warn">Network error while calling /compute/json: ${esc(String(err))}</div>`;
    return;
  }

  let rawText='';
  try{
    rawText=await res.text();
  }catch(err){
    document.getElementById('summary').innerHTML=`<div class="status-warn">Could not read backend response: ${esc(String(err))}</div>`;
    return;
  }

  let data=null;
  if(rawText){
    try{
      data=JSON.parse(rawText);
    }catch(err){
      document.getElementById('summary').innerHTML=`<div class="status-warn">Backend returned a non-JSON response. HTTP ${res.status}. Response starts with: ${esc(rawText.slice(0,300))}</div>`;
      return;
    }
  }

  if(!res.ok){
    const detail=(data && data.detail)?data.detail:`Request failed with HTTP ${res.status}`;
    document.getElementById('summary').innerHTML=`<div class="status-warn">${esc(String(detail))}</div>`;
    return;
  }

  try{
    renderSummary(data);
    renderGantt(data);
    renderNetwork(data);
    renderScheduleTable(data);
  }catch(err){
    document.getElementById('summary').innerHTML=`<div class="status-warn">Frontend rendering error: ${esc(String(err))}</div>`;
  }
}
function renderSummary(data){const path=(data.dominant_path||[]).join(' → ')||'—';document.getElementById('summary').innerHTML=`<div class='status-ok'><strong>Calculation complete</strong><div class='stats'><div class='stat'><div class='stat-label'>Lead time</div><div style='font-size:24px;font-weight:700;margin-top:4px'>${Math.round(data.lead_time)}</div></div><div class='stat'><div class='stat-label'>Processes</div><div style='font-size:24px;font-weight:700;margin-top:4px'>${(data.schedule||[]).length}</div></div></div><div class='stat' style='margin-top:10px;'><div class='stat-label'>Dominant top-level critical path</div><div style='margin-top:6px;font-weight:600;'>${esc(path)}</div></div></div>`;}
function buildDisplayOrder(schedule){
  const byParent={};
  (schedule||[]).forEach(r=>{const p=r.refines||'';(byParent[p]||(byParent[p]=[])).push(r);});
  Object.values(byParent).forEach(arr=>arr.sort((a,b)=>a.earliest_start-b.earliest_start||a.process.localeCompare(b.process)));
  const ordered=[];
  function visit(parent){
    (byParent[parent]||[]).forEach(row=>{
      ordered.push(row);
      visit(row.process);
    });
  }
  visit('');
  return ordered;
}
function renderGantt(data){
  const schedule=buildDisplayOrder(data.schedule||[]);
  const dominantSet=new Set(data.dominant_path||[]);
  const width=1380,rowHeight=42,labelWidth=260,rightPad=24,topPad=26,chartWidth=width-labelWidth-rightPad,height=topPad+schedule.length*rowHeight+30,finish=Math.max(data.lead_time||1,1),scale=value=>labelWidth+(value/finish)*chartWidth,fmt=value=>Math.round(value*1000)/1000;
  let svg=`<svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">`;
  const tickCount=Math.min(Math.round(finish)+1,21);
  for(let i=0;i<tickCount;i++){
    const tick=(finish/Math.max(tickCount-1,1))*i,x=scale(tick);
    svg+=`<line x1="${x}" y1="16" x2="${x}" y2="${height-16}" stroke="#e5e7eb" stroke-width="1" /><text x="${x}" y="12" text-anchor="middle" font-size="11" fill="#6b7280">${fmt(tick)}</text>`;
  }
  schedule.forEach((row,idx)=>{
    const y=topPad+idx*rowHeight,x=scale(row.earliest_start),w=Math.max(scale(row.earliest_finish)-x,6),hue=(idx*37)%360,fill=`hsl(${hue} 70% 65%)`,isDominant=dominantSet.has(row.process)&&row.depth===0,stroke=isDominant?'#dc2626':row.critical?'#111827':'transparent',sw=isDominant?3:row.critical?2:0,label=`${'— '.repeat(row.depth)}${row.process}`,details=`S:${fmt(row.earliest_start)} | D:${fmt(row.duration)} | E:${fmt(row.earliest_finish)}`;
    svg+=`<text x="10" y="${y+15}" font-size="12" fill="#111827">${esc(label)}</text><text x="10" y="${y+29}" font-size="10" fill="#475569">${esc(details)}</text><rect x="${x}" y="${y+4}" width="${w}" height="20" rx="8" fill="${fill}" stroke="${stroke}" stroke-width="${sw}" /><text x="${Math.min(x+6,x+w-4)}" y="${y+18}" font-size="10" fill="#111827">${esc(details)}</text>`;
  });
  svg+='</svg>';
  document.getElementById('ganttContainer').innerHTML=svg;
}
function computeLevels(graph){const incoming={},out={};graph.nodes.forEach(n=>{incoming[n.id]=[];out[n.id]=[];});graph.edges.forEach(e=>{incoming[e.to].push(e.from);out[e.from].push(e.to);});const levels={},queue=graph.nodes.filter(n=>incoming[n.id].length===0).map(n=>n.id).sort();queue.forEach(id=>levels[id]=0);while(queue.length){const id=queue.shift();out[id].forEach(next=>{const proposed=(levels[id]||0)+1;levels[next]=Math.max(levels[next]||0,proposed);incoming[next]=incoming[next].filter(x=>x!==id);if(incoming[next].length===0)queue.push(next);});}return levels;}






let networkViewport={scale:1,tx:0,ty:0,minScale:0.2,maxScale:3.5,contentW:1400,contentH:760};

function toggleParent(process){}
function setNetworkBadge(){
  const el=document.getElementById('networkZoomBadge');
  if(el) el.textContent=`Zoom: ${Math.round(networkViewport.scale*100)}%`;
}
function fitNetwork(){
  const host=document.getElementById('networkContainer');
  if(!host) return;
  const vw=Math.max(host.clientWidth-24,320), vh=656;
  const sx=vw/Math.max(networkViewport.contentW,1), sy=vh/Math.max(networkViewport.contentH,1);
  networkViewport.scale=Math.max(networkViewport.minScale,Math.min(networkViewport.maxScale,Math.min(sx,sy,1)));
  networkViewport.tx=(vw-networkViewport.contentW*networkViewport.scale)/2 + 12;
  networkViewport.ty=(vh-networkViewport.contentH*networkViewport.scale)/2 + 12;
  applyNetworkTransform();
}
function zoomNetwork(factor,cx=null,cy=null){
  const host=document.getElementById('networkContainer');
  if(!host) return;
  const rect=host.getBoundingClientRect();
  const px=(cx===null)?rect.width/2:(cx-rect.left);
  const py=(cy===null)?rect.height/2:(cy-rect.top);
  const oldScale=networkViewport.scale;
  const newScale=Math.max(networkViewport.minScale,Math.min(networkViewport.maxScale,oldScale*factor));
  if(newScale===oldScale) return;
  const worldX=(px-networkViewport.tx)/oldScale;
  const worldY=(py-networkViewport.ty)/oldScale;
  networkViewport.scale=newScale;
  networkViewport.tx=px-worldX*newScale;
  networkViewport.ty=py-worldY*newScale;
  applyNetworkTransform();
}
function applyNetworkTransform(){
  const g=document.getElementById('networkPanZoomGroup');
  if(g) g.setAttribute('transform',`translate(${networkViewport.tx},${networkViewport.ty}) scale(${networkViewport.scale})`);
  setNetworkBadge();
}
function installNetworkInteraction(){
  const host=document.getElementById('networkContainer');
  if(!host) return;
  let dragging=false,startX=0,startY=0,baseTx=0,baseTy=0;
  host.onwheel=(e)=>{e.preventDefault();zoomNetwork(e.deltaY<0?1.12:1/1.12,e.clientX,e.clientY);};
  host.onmousedown=(e)=>{
    dragging=true; startX=e.clientX; startY=e.clientY; baseTx=networkViewport.tx; baseTy=networkViewport.ty;
    host.style.cursor='grabbing';
  };
  window.onmousemove=(e)=>{
    if(!dragging) return;
    networkViewport.tx=baseTx+(e.clientX-startX);
    networkViewport.ty=baseTy+(e.clientY-startY);
    applyNetworkTransform();
  };
  window.onmouseup=()=>{dragging=false; if(host) host.style.cursor='grab';};
  host.style.cursor='grab';
}

function groupRowsByParent(schedule){
  const byParent={};
  (schedule||[]).forEach(r=>{const p=r.refines||'';(byParent[p]||(byParent[p]=[])).push(r);});
  Object.values(byParent).forEach(arr=>arr.sort((a,b)=>a.earliest_start-b.earliest_start||a.process.localeCompare(b.process)));
  return byParent;
}
function edgesForParent(graph, scheduleMap, parent){
  return (graph.edges||[]).filter(e=>(scheduleMap[e.from]?.refines||'')===parent && (scheduleMap[e.to]?.refines||'')===parent);
}
function computeLevels(rows, edges){
  const ids=rows.map(r=>r.process), incoming={}, outgoing={};
  ids.forEach(id=>{incoming[id]=[]; outgoing[id]=[];});
  edges.forEach(e=>{if(incoming[e.to]&&outgoing[e.from]){incoming[e.to].push(e.from); outgoing[e.from].push(e.to);}});
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
  ids.forEach(id=>{if(levels[id]===undefined) levels[id]=0;});
  return levels;
}
function computeLanes(rows, edges, levels){
  const byLevel={}; rows.forEach(r=>{const lvl=levels[r.process]||0;(byLevel[lvl]||(byLevel[lvl]=[])).push(r);});
  const laneOf={}, used={};
  Object.keys(byLevel).map(Number).sort((a,b)=>a-b).forEach(level=>{
    byLevel[level].sort((a,b)=>a.earliest_start-b.earliest_start||a.process.localeCompare(b.process));
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
function drawOrthoPath(points,color,strokeW,marker){
  const d=points.map((p,i)=>`${i===0?'M':'L'} ${p.x} ${p.y}`).join(' ');
  return `<path d="${d}" fill="none" stroke="${color}" stroke-width="${strokeW}" marker-end="url(#${marker})" stroke-linecap="round" stroke-linejoin="round" />`;
}
function simplifyPoints(points){
  const out=[points[0]];
  for(let i=1;i<points.length;i++){
    const p=points[i], q=out[out.length-1];
    if(p.x!==q.x || p.y!==q.y) out.push(p);
  }
  return out;
}


function renderNetwork(data,shouldFit=true){
  window.__lastNetworkData=data;
  const schedule=data.schedule||[];
  const scheduleMap={}; schedule.forEach(r=>scheduleMap[r.process]=r);
  const byParent=groupRowsByParent(schedule);
  const topRows=(byParent['']||[]).slice();
  const topEdges=edgesForParent(data.graph||{edges:[]}, scheduleMap, '');

  const levels=computeLevels(topRows, topEdges);
  const lanes=computeLanes(topRows, topEdges, levels);

  const cardW=180, cardH=76, colGap=180, laneGap=90, marginX=60, marginY=50;
  const positions={};
  let maxX=0,maxY=0;
  topRows.forEach(r=>{
    const x=marginX+(levels[r.process]||0)*(cardW+colGap);
    const y=marginY+(lanes[r.process]||0)*(cardH+laneGap);
    positions[r.process]={x,y,w:cardW,h:cardH};
    maxX=Math.max(maxX,x+cardW);
    maxY=Math.max(maxY,y+cardH);
  });

  networkViewport.contentW=Math.max(maxX+120,1320);
  networkViewport.contentH=Math.max(maxY+120,500);

  function rightMid(p){return {x:p.x+p.w,y:p.y+p.h/2};}
  function leftMid(p){return {x:p.x,y:p.y+p.h/2};}
  function topPoint(p,slot,total){
    const span=p.w*0.6;
    const start=p.x+(p.w-span)/2;
    const step=total>1?span/(total-1):0;
    return {x:start+slot*step,y:p.y};
  }
  function bottomPoint(p,slot,total){
    const span=p.w*0.6;
    const start=p.x+(p.w-span)/2;
    const step=total>1?span/(total-1):0;
    return {x:start+slot*step,y:p.y+p.h};
  }

  const dominantEdges=new Set((data.critical_edges||[]).map(e=>`${e.from}__${e.to}`));
  const dominantPath=new Set(data.dominant_path||[]);

  // Incoming edge groups by target
  const incomingByTarget={};
  topEdges.forEach(e=>{
    (incomingByTarget[e.to]||(incomingByTarget[e.to]=[])).push(e);
  });

  // Formal-ish side assignment based on locked rules:
  // - one incoming => left
  // - multiple incoming => distribute across top/left/bottom (never right)
  // - choose side from source geometry when possible
  const assignment={}; // key => {side, slot, sideCount}
  Object.entries(incomingByTarget).forEach(([target, arr])=>{
    const tgt=positions[target];
    const tgtCy=tgt.y+tgt.h/2;
    const classified={top:[], left:[], bottom:[]};

    arr.forEach(edge=>{
      const src=positions[edge.from];
      const srcCy=src.y+src.h/2;
      if(arr.length===1){
        classified.left.push(edge);
      }else if(srcCy < tgtCy - 20){
        classified.top.push(edge);
      }else if(srcCy > tgtCy + 20){
        classified.bottom.push(edge);
      }else{
        classified.left.push(edge);
      }
    });

    // rebalance if one bucket is empty and another is overcrowded
    const orderedSides=['top','left','bottom'];
    if(arr.length>1 && classified.left.length===0){
      const donor = classified.top.length > classified.bottom.length ? 'top' : 'bottom';
      if(classified[donor].length){
        classified.left.push(classified[donor].shift());
      }
    }

    orderedSides.forEach(side=>{
      classified[side].sort((a,b)=>{
        const aCrit=dominantEdges.has(`${a.from}__${a.to}`)?-1:0;
        const bCrit=dominantEdges.has(`${b.from}__${b.to}`)?-1:0;
        if(aCrit!==bCrit) return aCrit-bCrit;
        const ay=positions[a.from].y;
        const by=positions[b.from].y;
        return ay-by;
      });
      const total=classified[side].length;
      classified[side].forEach((edge,idx)=>{
        assignment[`${edge.from}__${edge.to}`]={side,slot:idx,total};
      });
    });
  });

  // Dedicated horizontal channels per layer gap for outgoing travel
  const layerChannelY={};
  Object.keys(levels).forEach(proc=>{
    const lvl=levels[proc]||0;
    if(layerChannelY[lvl]===undefined){
      layerChannelY[lvl]=marginY - 24 + lvl*6;
    }
  });

  function routeEdge(edge){
    const from=positions[edge.from], to=positions[edge.to];
    const a=assignment[`${edge.from}__${edge.to}`] || {side:'left',slot:0,total:1};

    const start=rightMid(from); // outgoing always right
    let end;
    if(a.side==='left') end=leftMid(to);
    else if(a.side==='top') end=topPoint(to,a.slot,a.total);
    else end=bottomPoint(to,a.slot,a.total);

    const srcLayer=levels[edge.from]||0;
    const targetApproachGap=46;
    const sourceRun=34;
    const slotDelta=16;
    const channelY=(layerChannelY[srcLayer]||start.y) + a.slot*4;

    let pts=[start];

    if(a.side==='left'){
      const approachX=end.x - targetApproachGap - a.slot*slotDelta;
      const runX=Math.max(start.x + sourceRun, approachX - 48);
      pts.push({x:runX,y:start.y});
      pts.push({x:approachX,y:start.y});
      pts.push({x:approachX,y:end.y});
      pts.push(end);
    } else if(a.side==='top'){
      const approachY=end.y - targetApproachGap - a.slot*slotDelta;
      const bendX=Math.max(start.x + sourceRun, end.x - 52 + a.slot*8);
      pts.push({x:bendX,y:start.y});
      pts.push({x:bendX,y:approachY});
      pts.push({x:end.x,y:approachY});
      pts.push(end);
    } else { // bottom
      const approachY=end.y + targetApproachGap + a.slot*slotDelta;
      const bendX=Math.max(start.x + sourceRun, end.x - 52 + a.slot*8);
      pts.push({x:bendX,y:start.y});
      pts.push({x:bendX,y:approachY});
      pts.push({x:end.x,y:approachY});
      pts.push(end);
    }

    const compact=[pts[0]];
    for(let i=1;i<pts.length;i++){
      const p=pts[i], q=compact[compact.length-1];
      if(p.x!==q.x || p.y!==q.y) compact.push(p);
    }
    return compact;
  }

  function segsFromPoints(points){
    const segs=[];
    for(let i=0;i<points.length-1;i++){
      segs.push([points[i],points[i+1]]);
    }
    return segs;
  }

  function segmentsCross(s1,s2){
    const [a,b]=s1,[c,d]=s2;
    const aVert=a.x===b.x, cVert=c.x===d.x;
    if(aVert===cVert) return false;
    const vert=aVert?[a,b]:[c,d];
    const hor=aVert?[c,d]:[a,b];
    const vx=vert[0].x;
    const hy=hor[0].y;
    const vMin=Math.min(vert[0].y,vert[1].y), vMax=Math.max(vert[0].y,vert[1].y);
    const hMin=Math.min(hor[0].x,hor[1].x), hMax=Math.max(hor[0].x,hor[1].x);
    return vx>hMin && vx<hMax && hy>vMin && hy<vMax;
  }

  function validateRoutes(routeMap){
    const keys=Object.keys(routeMap);
    for(let i=0;i<keys.length;i++){
      for(let j=i+1;j<keys.length;j++){
        const k1=keys[i], k2=keys[j];
        const s1=segsFromPoints(routeMap[k1]);
        const s2=segsFromPoints(routeMap[k2]);
        for(const seg1 of s1){
          for(const seg2 of s2){
            if(segmentsCross(seg1,seg2)) return {ok:false, reason:`crossing:${k1}:${k2}`};
          }
        }
      }
    }
    return {ok:true, reason:''};
  }

  const routeMap={};
  // non-critical first, critical last so critical is foreground
  const sortedEdges=[...topEdges].sort((a,b)=>{
    const aCrit=dominantEdges.has(`${a.from}__${a.to}`)?1:0;
    const bCrit=dominantEdges.has(`${b.from}__${b.to}`)?1:0;
    return aCrit-bCrit;
  });
  sortedEdges.forEach(e=>{
    routeMap[`${e.from}__${e.to}`]=routeEdge(e);
  });

  // simple repair loop: if crossings found, push lower-priority edge approaches farther from target
  for(let repair=0; repair<8; repair++){
    const check=validateRoutes(routeMap);
    if(check.ok) break;
    const parts=check.reason.split(':');
    const loserKey=parts[3] || parts[2];
    if(assignment[loserKey]){
      assignment[loserKey].slot += 1;
      const [fromId,toId]=loserKey.split('__');
      routeMap[loserKey]=routeEdge({from:fromId,to:toId});
    }
  }

  function cardMarkup(node,row,isDominant){
    const es=Math.round((row.earliest_start||0)*1000)/1000;
    const ef=Math.round((row.earliest_finish||0)*1000)/1000;
    const dur=Math.round((row.duration||0)*1000)/1000;
    const stroke=isDominant?'#dc2626':'#334155';
    const strokeW=isDominant?2.6:1.2;
    return `<rect x="${node.x}" y="${node.y}" width="${node.w}" height="${node.h}" rx="8" fill="#ffffff" stroke="${stroke}" stroke-width="${strokeW}" />`+
      `<text x="${node.x+node.w/2}" y="${node.y+24}" text-anchor="middle" font-size="13" fill="#111827">${esc(row.process)}</text>`+
      `<text x="${node.x+10}" y="${node.y+51}" font-size="10" fill="#64748b">Start ${es}</text>`+
      `<text x="${node.x+node.w/2}" y="${node.y+51}" text-anchor="middle" font-size="10" fill="#64748b">Dur ${dur}</text>`+
      `<text x="${node.x+node.w-10}" y="${node.y+51}" text-anchor="end" font-size="10" fill="#64748b">End ${ef}</text>`;
  }

  let svg=`<svg id="networkSvgRoot" width="100%" height="680" viewBox="0 0 ${networkViewport.contentW} ${networkViewport.contentH}" preserveAspectRatio="xMidYMid meet"><defs><marker id="arrow" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#5b6b80" /></marker><marker id="arrowRed" markerWidth="10" markerHeight="10" refX="8" refY="3" orient="auto" markerUnits="strokeWidth"><path d="M0,0 L0,6 L9,3 z" fill="#dc2626" /></marker><pattern id="grid" width="24" height="24" patternUnits="userSpaceOnUse"><path d="M 24 0 L 0 0 0 24" fill="none" stroke="#f3f6f9" stroke-width="1"/></pattern></defs><rect x="0" y="0" width="${networkViewport.contentW}" height="${networkViewport.contentH}" fill="#fcfdff"/><rect x="0" y="0" width="${networkViewport.contentW}" height="${networkViewport.contentH}" fill="url(#grid)"/><g id="networkPanZoomGroup">`;

  sortedEdges.forEach(e=>{
    const key=`${e.from}__${e.to}`;
    const pts=routeMap[key];
    if(!pts || !pts.length) return;
    const isDom=dominantEdges.has(key);
    svg+=drawOrthoPath(pts,isDom?'#dc2626':'#6b7c93',isDom?2.9:1.9,isDom?'arrowRed':'arrow');
  });

  Object.entries(positions).forEach(([proc,node])=>{
    svg+=cardMarkup(node,scheduleMap[proc],dominantPath.has(proc));
  });

  svg+=`</g></svg>`;
  document.getElementById('networkContainer').innerHTML=svg;
  if(shouldFit){fitNetwork();}else{applyNetworkTransform();}
  installNetworkInteraction();
}

function renderScheduleTable(data){
  const schedule=data.schedule||[],table=document.getElementById('scheduleTable');
  let html=`<thead><tr><th>Process</th><th>Refines</th><th>Depth</th><th>Duration token</th><th>Resolved duration</th><th>ES</th><th>EF</th><th>LS</th><th>LF</th><th>Float</th><th>Critical</th></tr></thead><tbody>`;
  schedule.forEach(r=>{
    html+=`<tr><td>${esc(r.process)}</td><td>${esc(r.refines)}</td><td>${r.depth}</td><td>${esc(r.duration_raw)}</td><td>${Math.round((r.duration||0)*1000)/1000}</td><td>${Math.round((r.earliest_start||0)*1000)/1000}</td><td>${Math.round((r.earliest_finish||0)*1000)/1000}</td><td>${Math.round((r.latest_start||0)*1000)/1000}</td><td>${Math.round((r.latest_finish||0)*1000)/1000}</td><td>${Math.round((r.total_float||0)*1000)/1000}</td><td>${r.critical?'Yes':'No'}</td></tr>`;
  });
  html+='</tbody>';
  table.innerHTML=html;
}

renderAll();document.getElementById('summary').innerHTML='<div class="status-warn">Press Run to calculate.</div>';
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
        formula_map = build_formula_map(pd.DataFrame([r.model_dump() for r in payload.formulas])) if payload.formulas else {}
        df = dataframe_from_rows(payload.rows, formula_map)
        return JSONResponse(compute_hierarchical_schedule(df))
    except (PertDataError, CycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc

@app.post("/compute/excel")
async def compute_from_excel(file: UploadFile = File(...)) -> JSONResponse:
    try:
        content = await file.read()
        df, _ = load_pert_excel_from_bytes(content)
        return JSONResponse(compute_hierarchical_schedule(df))
    except (PertDataError, CycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc

@app.post("/schedule-excel")
def export_schedule_excel(payload: ComputeRequest) -> StreamingResponse:
    try:
        formula_map = build_formula_map(pd.DataFrame([r.model_dump() for r in payload.formulas])) if payload.formulas else {}
        df = dataframe_from_rows(payload.rows, formula_map)
        schedule_df = pd.DataFrame(compute_hierarchical_schedule(df)["schedule"])
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            schedule_df.to_excel(writer, sheet_name="Calculated_Schedule", index=False)
        output.seek(0)
        headers = {"Content-Disposition": 'attachment; filename="PERT_calculated_schedule.xlsx"'}
        return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers=headers)
    except (PertDataError, CycleError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {exc}") from exc

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
