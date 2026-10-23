import os
from typing import List, Optional, Dict, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ortools.sat.python import cp_model
from pydantic import BaseModel

app = FastAPI(title="Timetable Dynamic Solver API")

# CORS設定（ブラウザからの直接アクセスを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- リクエストデータモデル ---
class Teacher(BaseModel):
    id: str
    name: str
    type: str  # HEAD, FULL_TIME, PART_TIME, NATIVE
    research_day: Optional[int] = None
    available_days: Optional[List[int]] = None

class ClassItem(BaseModel):
    id: str
    name: str
    grade: Optional[int] = None
    homeroom_teacher_id: Optional[str] = None
    sub_teacher_id: Optional[str] = None

class Facility(BaseModel):
    type: str       # gym, ground, special_room など
    capacity: int   # 同時開講可能クラス数の上限

class Curriculum(BaseModel):
    class_id: str
    subject_id: str
    teacher_id: str
    weekly_slots: int
    facility_type: Optional[str] = None
    is_consecutive: bool = False  # 2コマ連続指定

class LockedSlot(BaseModel):
    teacher_id: str
    class_id: str
    day: int      # 1:月 〜 6:土
    period: int   # 0:H, 1〜6限

class DynamicRule(BaseModel):
    id: str
    enabled: bool = True
    params: Dict[str, Any] = {}

class SolveRequest(BaseModel):
    teachers: List[Teacher]
    classes: List[ClassItem]
    curriculums: List[Curriculum]
    facilities: List[Facility] = []
    locked_slots: List[LockedSlot] = []
    rules: List[DynamicRule] = []

# --- 動的ルールハンドラー群 ---

def rule_teacher_daily_max(model, assignments, valid_slots, req, params):
    """教員の1日最大担当コマ数制限"""
    max_periods = params.get("max_periods", 4)
    days = set(d for d, _ in valid_slots)
    for t in req.teachers:
        for d in days:
            slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] in [1, 2, 3, 4, 5, 6]]
            if slots:
                model.Add(sum(slots) <= max_periods)

def rule_forbidden_consecutive_periods(model, assignments, valid_slots, req, params):
    """連続担当禁止（デフォルト: 4限と5限）"""
    periods = params.get("periods", [4, 5])
    if len(periods) == 2:
        p1, p2 = periods[0], periods[1]
        days = set(d for d, _ in valid_slots)
        for t in req.teachers:
            for d in days:
                if (d, p1) in valid_slots and (d, p2) in valid_slots:
                    s1 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == p1]
                    s2 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == p2]
                    if s1 and s2:
                        model.Add(sum(s1) + sum(s2) <= 1)

def rule_part_time_restriction(model, assignments, valid_slots, req, params):
    """非常勤講師の拘束制限（1限と6限の両方配置を禁止）"""
    days = set(d for d, _ in valid_slots)
    for t in req.teachers:
        if t.type == "PART_TIME":
            for d in days:
                if (d, 1) in valid_slots and (d, 6) in valid_slots:
                    p1 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 1]
                    p6 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 6]
                    if p1 and p6:
                        model.Add(sum(p1) + sum(p6) <= 1)

def rule_facility_capacity(model, assignments, valid_slots, req, params):
    """施設（体育館・グラウンド等）の同時使用上限"""
    for fac in req.facilities:
        for d, p in valid_slots:
            fac_slots = [
                v for k, v in assignments.items()
                if k[5] == fac.type and k[3] == d and k[4] == p
            ]
            if fac_slots:
                model.Add(sum(fac_slots) <= fac.capacity)

def rule_teacher_research_day(model, assignments, valid_slots, req, params):
    """教員指定の研究日には授業を配置しない"""
    for t in req.teachers:
        if t.research_day:
            research_slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == t.research_day]
            if research_slots:
                model.Add(sum(research_slots) == 0)

def rule_subject_daily_limit(model, assignments, valid_slots, req, params):
    """同一クラスで同一科目は1日1コマまで（連続指定を除く）"""
    days = set(d for d, _ in valid_slots)
    for c in req.classes:
        for curr in req.curriculums:
            if curr.class_id == c.id and not curr.is_consecutive:
                for d in days:
                    slots = [
                        v for k, v in assignments.items()
                        if k[2] == c.id and k[6] == curr.subject_id and k[3] == d and k[4] in [1, 2, 3, 4, 5, 6]
                    ]
                    if slots:
                        model.Add(sum(slots) <= 1)

# ルールIDとハンドラー関数のマッピング辞書
RULE_HANDLERS = {
    "TEACHER_DAILY_MAX": rule_teacher_daily_max,
    "FORBIDDEN_CONSECUTIVE_PERIODS": rule_forbidden_consecutive_periods,
    "PART_TIME_RESTRICTION": rule_part_time_restriction,
    "FACILITY_CAPACITY": rule_facility_capacity,
    "TEACHER_RESEARCH_DAY": rule_teacher_research_day,
    "SUBJECT_DAILY_LIMIT": rule_subject_daily_limit,
}

# --- 最適化エンドポイント ---
@app.post("/api/solve")
def solve_timetable(req: SolveRequest):
    model = cp_model.CpModel()

    days = [1, 2, 3, 4, 5, 6]  # 月〜土
    periods = [0, 1, 2, 3, 4, 5, 6]  # 0:H, 1〜6限
    valid_slots = [(d, p) for d in days for p in periods if not (d == 6 and p > 4)]

    # 1. 決定変数の作成
    # assignments[(c_key, teacher_id, class_id, day, period, facility_type, subject_id)]
    assignments = {}
    for curr in req.curriculums:
        for slot_idx in range(curr.weekly_slots):
            c_key = f"{curr.class_id}_{curr.subject_id}_{slot_idx}"
            for d, p in valid_slots:
                assignments[(c_key, curr.teacher_id, curr.class_id, d, p, curr.facility_type, curr.subject_id)] = model.NewBoolVar(
                    f"x_{c_key}_{d}_{p}"
                )

    # 2. 絶対公理1: 各科目の必要コマ数を完全に満たす
    for curr in req.curriculums:
        for slot_idx in range(curr.weekly_slots):
            c_key = f"{curr.class_id}_{curr.subject_id}_{slot_idx}"
            model.Add(
                sum(assignments[(c_key, curr.teacher_id, curr.class_id, d, p, curr.facility_type, curr.subject_id)]
                    for d, p in valid_slots) == 1
            )

    # 3. 絶対公理2: 同一教員の同時限重複禁止
    for t in req.teachers:
        for d, p in valid_slots:
            teacher_slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == p]
            if teacher_slots:
                model.Add(sum(teacher_slots) <= 1)

    # 4. 絶対公理3: 同一クラスの同時限重複禁止
    for c in req.classes:
        for d, p in valid_slots:
            class_slots = [v for k, v in assignments.items() if k[2] == c.id and k[3] == d and k[4] == p]
            if class_slots:
                model.Add(sum(class_slots) <= 1)

    # 5. ピン留め（固定枠）の維持
    for lock in req.locked_slots:
        target_slots = [
            v for k, v in assignments.items()
            if k[1] == lock.teacher_id and k[2] == lock.class_id and k[3] == lock.day and k[4] == lock.period
        ]
        if target_slots:
            model.Add(sum(target_slots) == 1)

    # 6. Lovableから送られてきた動的ルールの適用
    for rule in req.rules:
        if rule.enabled and rule.id in RULE_HANDLERS:
            RULE_HANDLERS[rule.id](model, assignments, valid_slots, req, rule.params)

    # 7. ソ
