from collections import defaultdict
from typing import Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from ortools.sat.python import cp_model
from pydantic import BaseModel, Field, model_validator

app = FastAPI(title="Timetable Solver API")

# Lovableからのブラウザ間通信を許可するCORS設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class Slot(BaseModel):
    day: int = Field(ge=1, le=6)
    period: int = Field(ge=1, le=6)

class Teacher(BaseModel):
    id: str
    name: str
    type: str
    research_day: Optional[int] = Field(default=None, ge=1, le=6)
    availability: Optional[list[Slot]] = None

class ClassItem(BaseModel):
    id: str
    name: str

class Facility(BaseModel):
    id: Optional[str] = None
    type: Optional[str] = None
    capacity: int = Field(ge=1)

class Curriculum(BaseModel):
    class_id: str
    subject_id: str
    course_id: Optional[str] = None
    teacher_id: Optional[str] = None
    teacher_ids: list[str] = Field(default_factory=list)
    weekly_slots: int = Field(ge=0)
    consecutive: int = Field(default=1, ge=1, le=2)
    is_consecutive: Optional[bool] = None
    facility_type: Optional[str] = None
    subject_name: Optional[str] = None
    room_id: Optional[str] = None
    room_usage: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def teachers_present(self):
        if self.is_consecutive is not None:
            self.consecutive = 2 if self.is_consecutive else 1
        self.teacher_ids = list(dict.fromkeys(self.teacher_ids or ([self.teacher_id] if self.teacher_id else [])))
        if not self.teacher_ids:
            raise ValueError("At least one teacher is required")
        if self.teacher_id and self.teacher_id not in self.teacher_ids:
            raise ValueError("teacher_id must belong to teacher_ids")
        return self

class LockedSlot(Slot):
    teacher_id: Optional[str] = None
    teacher_ids: list[str] = Field(default_factory=list)
    class_id: str
    subject_id: Optional[str] = None
    course_id: Optional[str] = None

class SolveRequest(BaseModel):
    teachers: list[Teacher]
    classes: list[ClassItem]
    facilities: list[Facility] = Field(default_factory=list)
    curriculums: list[Curriculum]
    locked_slots: list[LockedSlot] = Field(default_factory=list)
    lesson_slots: list[Slot] = Field(default_factory=lambda: [Slot(day=d, period=p) for d in range(1,7) for p in range(1,5 if d==6 else 7)])
    rules: Optional[list["DynamicRule"]] = None

class DynamicRule(BaseModel):
    id: str
    enabled: bool = True
    params: dict = Field(default_factory=dict)
    level: str = "HARD"
    weight: int = Field(default=10, ge=0)

SolveRequest.model_rebuild()

@app.post("/api/solve")
def solve_timetable(req: SolveRequest):
    invalid = lambda: {"status": "INVALID_REQUEST", "timetable": []}
    teachers = {t.id: t for t in req.teachers}
    classes = {c.id for c in req.classes}
    facilities = {r.id or r.type: r.capacity for r in req.facilities}
    if len(facilities) != len(req.facilities):
        return invalid()
    if len(teachers) != len(req.teachers) or len(classes) != len(req.classes):
        return invalid()
    slots = sorted({(s.day, s.period) for s in req.lesson_slots})
    rules = {r.id:r for r in (req.rules or []) if r.enabled}
    if len(rules) != len([r for r in (req.rules or []) if r.enabled]):
        return invalid()
    legacy = req.rules is None
    enabled = lambda code: legacy or code in rules
    daily_max = rules['TEACHER_DAILY_MAX'].params.get('max_periods',4) if 'TEACHER_DAILY_MAX' in rules else 4
    forbidden = rules['FORBIDDEN_CONSECUTIVE_PERIODS'].params.get('periods',[4,5]) if 'FORBIDDEN_CONSECUTIVE_PERIODS' in rules else [4,5]
    if type(daily_max) is not int or not 1<=daily_max<=6 or not isinstance(forbidden,list) or len(forbidden)!=2 or len(set(forbidden))!=2 or any(type(p) is not int or not 1<=p<=6 for p in forbidden):
        return invalid()
    lessons = {}
    for c in req.curriculums:
        key = c.course_id or f"{c.class_id}|{c.subject_id}"
        if c.class_id not in classes or any(t not in teachers for t in c.teacher_ids):
            return invalid()
        spec = (c.subject_id, tuple(sorted(c.teacher_ids)), c.weekly_slots, c.consecutive)
        room = c.room_id
        if not room and c.facility_type:
            matches=[r.id or r.type for r in req.facilities if r.type==c.facility_type]
            if len(matches)!=1: return invalid()
            room=matches[0]
        if room and room not in facilities:
            return invalid()
        if key in lessons:
            if lessons[key]["spec"] != spec or c.class_id in lessons[key]["classes"] or lessons[key]["room"] != (room,c.room_usage):
                return invalid()
            lessons[key]["classes"].add(c.class_id)
        else:
            lessons[key] = {"spec": spec, "classes": {c.class_id}, "course_id": c.course_id, "room": (room,c.room_usage), "subject_name":c.subject_name or c.subject_id}
    model = cp_model.CpModel()
    penalties=[]
    def limit(expression, maximum, code):
        rule=rules.get(code)
        if rule and rule.level=='SOFT':
            excess=model.NewIntVar(0,100000,f'penalty_{len(penalties)}')
            model.Add(excess>=expression-maximum)
            penalties.append(excess*rule.weight)
        else:
            model.Add(expression<=maximum)
    variables = {}
    teacher_slot, class_slot = defaultdict(list), defaultdict(list)
    room_slot = defaultdict(list)
    for key, lesson in lessons.items():
        subject, tids, count, consecutive = lesson["spec"]
        for d, p in slots:
            v = model.NewBoolVar(f"x_{key}_{d}_{p}")
            variables[key, d, p] = v
            room, usage = lesson["room"]
            if room:
                room_slot[room,d,p].append(usage * v)
            for tid in tids:
                t = teachers[tid]
                teacher_slot[tid,d,p].append(v)
                if t.type != "PART_TIME" and t.research_day == d and enabled('TEACHER_RESEARCH_DAY'):
                    limit(v,0,'TEACHER_RESEARCH_DAY')
                if t.availability is not None and (d,p) not in {(s.day,s.period) for s in t.availability} and enabled('PART_TIME_RESTRICTION'):
                    limit(v,0,'PART_TIME_RESTRICTION')
            for cid in lesson["classes"]:
                class_slot[cid,d,p].append(v)
        model.Add(sum(variables[key,d,p] for d,p in slots) == count)
        if consecutive == 2:
            pairs = []
            for d,p in slots:
                if p in (1,2,5) and (d,p+1) in slots:
                    pair = model.NewBoolVar(f"pair_{key}_{d}_{p}")
                    pairs.append((d,p,pair))
            for d,p in slots:
                model.Add(variables[key,d,p] == sum(v for day,start,v in pairs if day==d and p in (start,start+1)))
    for vs in list(teacher_slot.values()) + list(class_slot.values()):
        model.Add(sum(vs) <= 1)
    for (room,d,p), vs in room_slot.items():
        if enabled('FACILITY_CAPACITY'): limit(sum(vs), facilities[room], 'FACILITY_CAPACITY')
    for t in req.teachers:
        for d in range(1,7):
            load=sum(v for (tid,day,p),vs in teacher_slot.items() if tid==t.id and day==d for v in vs)
            if t.type=='PART_TIME':
                if enabled('PART_TIME_RESTRICTION'):
                    limit(load,5,'PART_TIME_RESTRICTION')
                    limit(sum(teacher_slot[t.id,d,1]+teacher_slot[t.id,d,6]),1,'PART_TIME_RESTRICTION')
            else:
                if enabled('TEACHER_DAILY_MAX'): limit(load,daily_max,'TEACHER_DAILY_MAX')
                if enabled('FORBIDDEN_CONSECUTIVE_PERIODS'):
                    a,b=forbidden
                    limit(sum(teacher_slot[t.id,d,a]+teacher_slot[t.id,d,b]),1,'FORBIDDEN_CONSECUTIVE_PERIODS')
    if not legacy and enabled('SUBJECT_DAILY_LIMIT'):
        subject_day=defaultdict(list)
        for key,l in lessons.items():
            for cid in l['classes']:
                for d,p in slots: subject_day[cid,l['subject_name'],d].append(variables[key,d,p])
        for (cid,subject,d), vs in subject_day.items():
            consecutive=any(l['subject_name']==subject and cid in l['classes'] and l['spec'][3]==2 for l in lessons.values())
            limit(sum(vs),2 if consecutive else 1,'SUBJECT_DAILY_LIMIT')
    for lock in req.locked_slots:
        if (lock.day,lock.period) not in slots:
            return invalid()
        candidates = [key for key,l in lessons.items() if lock.class_id in l["classes"] and (not lock.course_id or l["course_id"]==lock.course_id) and (not lock.subject_id or l["spec"][0]==lock.subject_id) and (not lock.teacher_id or lock.teacher_id in l["spec"][1]) and set(lock.teacher_ids).issubset(l["spec"][1])]
        if len(candidates)!=1:
            return invalid()
        model.Add(variables[candidates[0],lock.day,lock.period] == 1)
    solver = cp_model.CpSolver()
    if penalties: model.Minimize(sum(penalties))
    solver.parameters.max_time_in_seconds = 30
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "INFEASIBLE" if status==cp_model.INFEASIBLE else "UNKNOWN", "timetable": []}
    results = []
    seen_teachers, seen_classes = set(),set()
    counts = defaultdict(int)
    for (key,d,p),v in variables.items():
        if not solver.Value(v):
            continue
        l = lessons[key]
        subject,tids,_,_ = l["spec"]
        if p==0 or (d,p) not in slots or any((t,d,p) in seen_teachers for t in tids) or any((c,d,p) in seen_classes for c in l["classes"]):
            return {"status": "ERROR", "timetable": []}
        for tid in tids:
            t=teachers[tid]
            research_hard=enabled('TEACHER_RESEARCH_DAY') and ('TEACHER_RESEARCH_DAY' not in rules or rules['TEACHER_RESEARCH_DAY'].level!='SOFT')
            available_hard=enabled('PART_TIME_RESTRICTION') and ('PART_TIME_RESTRICTION' not in rules or rules['PART_TIME_RESTRICTION'].level!='SOFT')
            if (research_hard and t.type!="PART_TIME" and t.research_day==d) or (available_hard and t.availability is not None and (d,p) not in {(s.day,s.period) for s in t.availability}):
                return {"status":"ERROR","timetable":[]}
        seen_teachers.update((t,d,p) for t in tids)
        seen_classes.update((c,d,p) for c in l["classes"])
        counts[key]+=1
        for cid in sorted(l["classes"]):
            results.append({"teacher_id":tids[0],"teacher_ids":list(tids),"class_id":cid,"subject_id":subject,"course_id":l["course_id"],"day":d,"period":p})
    if any(counts[key]!=l["spec"][2] for key,l in lessons.items()):
        return {"status":"ERROR","timetable":[]}
    return {"status":"SUCCESS","timetable":results}

@app.get("/")
def health_check():
    return {"status":"healthy"}
