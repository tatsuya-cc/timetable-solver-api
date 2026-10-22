import os
from typing import List, Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ortools.sat.python import cp_model
from pydantic import BaseModel

app = FastAPI(title="Timetable Solver API")

# Lovableからの通信を許可するCORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Teacher(BaseModel):
    id: str
    name: str
    type: str  # HEAD, FULL_TIME, PART_TIME, NATIVE
    research_day: Optional[int] = None

class ClassItem(BaseModel):
    id: str
    name: str

class Curriculum(BaseModel):
    class_id: str
    subject_id: str
    teacher_id: str
    weekly_slots: int

class LockedSlot(BaseModel):
    teacher_id: str
    class_id: str
    day: int      # 1:月 〜 6:土
    period: int   # 0:H, 1〜6限

class SolveRequest(BaseModel):
    teachers: List[Teacher]
    classes: List[ClassItem]
    curriculums: List[Curriculum]
    locked_slots: List[LockedSlot] = []

@app.post("/api/solve")
def solve_timetable(req: SolveRequest):
    model = cp_model.CpModel()

    days = [1, 2, 3, 4, 5, 6]
    periods = [0, 1, 2, 3, 4, 5, 6]
    valid_slots = [(d, p) for d in days for p in periods if not (d == 6 and p > 4)]

    # 1. 決定変数の作成
    assignments = {}
    for curr in req.curriculums:
        for slot_idx in range(curr.weekly_slots):
            c_key = f"{curr.class_id}_{curr.subject_id}_{slot_idx}"
            for d, p in valid_slots:
                assignments[(c_key, curr.teacher_id, curr.class_id, d, p)] = model.NewBoolVar(
                    f"x_{c_key}_{d}_{p}"
                )

    # 2. 各科目の必要コマ数を満たす
    for curr in req.curriculums:
        for slot_idx in range(curr.weekly_slots):
            c_key = f"{curr.class_id}_{curr.subject_id}_{slot_idx}"
            model.Add(
                sum(assignments[(c_key, curr.teacher_id, curr.class_id, d, p)] for d, p in valid_slots) == 1
            )

    # 3. 教員の同時重複禁止
    for t in req.teachers:
        for d, p in valid_slots:
            teacher_slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == p]
            if teacher_slots:
                model.Add(sum(teacher_slots) <= 1)

    # 4. クラスの同時重複禁止
    for c in req.classes:
        for d, p in valid_slots:
            class_slots = [v for k, v in assignments.items() if k[2] == c.id and k[3] == d and k[4] == p]
            if class_slots:
                model.Add(sum(class_slots) <= 1)

    # 5. 4限・5限の連続担当禁止
    for t in req.teachers:
        for d in days:
            if (d, 4) in valid_slots and (d, 5) in valid_slots:
                p4 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 4]
                p5 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 5]
                if p4 and p5:
                    model.Add(sum(p4) + sum(p5) <= 1)

    # 6. 教員1日最大4コマまで
    for t in req.teachers:
        for d in days:
            daily_slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] in [1, 2, 3, 4, 5, 6]]
            if daily_slots:
                model.Add(sum(daily_slots) <= 4)

    # 7. 講師拘束制限（1限と6限の両方担当を禁止）
    for t in req.teachers:
        if t.type == "PART_TIME":
            for d in days:
                if (d, 1) in valid_slots and (d, 6) in valid_slots:
                    p1 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 1]
                    p6 = [v for k, v in assignments.items() if k[1] == t.id and k[3] == d and k[4] == 6]
                    if p1 and p6:
                        model.Add(sum(p1) + sum(p6) <= 1)

    # 8. 研究日には授業を入れない
    for t in req.teachers:
        if t.research_day:
            research_slots = [v for k, v in assignments.items() if k[1] == t.id and k[3] == t.research_day]
            if research_slots:
                model.Add(sum(research_slots) == 0)

    # 9. ピン留め（固定枠）の維持
    for lock in req.locked_slots:
        target_slots = [
            v for k, v in assignments.items()
            if k[1] == lock.teacher_id and k[2] == lock.class_id and k[3] == lock.day and k[4] == lock.period
        ]
        if target_slots:
            model.Add(sum(target_slots) == 1)

    # ソルバー実行
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)

    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        results = []
        for k, v in assignments.items():
            if solver.Value(v) == 1:
                results.append({
                    "teacher_id": k[1],
                    "class_id": k[2],
                    "day": k[3],
                    "period": k[4]
                })
        return {"status": "SUCCESS", "timetable": results}
    else:
        return {"status": "INFEASIBLE", "message": "制約条件を満たす配置が見つかりませんでした。"}

@app.get("/")
def health_check():
    return {"status": "healthy"}
