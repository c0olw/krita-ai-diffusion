from __future__ import annotations
from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime
from enum import Enum, Flag
from typing import Deque, NamedTuple
from PyQt5.QtCore import QObject, pyqtSignal

from .image import Bounds, ImageCollection
from .settings import settings
from .util import ensure
from . import control


class JobState(Flag):
    queued = 0
    executing = 1
    finished = 2
    cancelled = 3


class JobKind(Enum):
    diffusion = 0
    control_layer = 1
    upscaling = 2
    live_preview = 3


@dataclass
class JobParams:
    bounds: Bounds
    prompt: str
    negative_prompt: str = ""
    strength: float = 1.0
    seed: int = 0

    @classmethod
    def equal_ignore_seed(cls, a: JobParams | None, b: JobParams | None):
        if a is None or b is None:
            return a is b
        field_names = (f.name for f in fields(cls) if not f.name == "seed")
        return all(getattr(a, name) == getattr(b, name) for name in field_names)


class Job:
    id: str | None
    kind: JobKind
    state = JobState.queued
    params: JobParams
    control: "control.ControlLayer | None" = None
    timestamp: datetime
    results: ImageCollection
    _in_use: dict[int, bool]

    def __init__(self, id: str | None, kind: JobKind, params: JobParams):
        self.id = id
        self.kind = kind
        self.params = params
        self.timestamp = datetime.now()
        self.results = ImageCollection()
        self._in_use = {}

    def result_was_used(self, index: int):
        return self._in_use.get(index, False)


class JobQueue(QObject):
    """Queue of waiting, ongoing and finished jobs for one document."""

    class Item(NamedTuple):
        job: str
        image: int

    count_changed = pyqtSignal()
    selection_changed = pyqtSignal()
    job_finished = pyqtSignal(Job)
    job_discarded = pyqtSignal(Job)
    result_used = pyqtSignal(Item)

    _entries: Deque[Job]
    _selection: Item | None = None
    _memory_usage = 0  # in MB

    def __init__(self):
        super().__init__()
        self._entries = deque()

    def add(
        self,
        kind: JobKind,
        id: str,
        prompt: str,
        negative: str,
        bounds: Bounds,
        strength: float,
        seed: int,
    ):
        return self.add_job(Job(id, kind, JobParams(bounds, prompt, negative, strength, seed)))

    def add_control(self, control: "control.ControlLayer", bounds: Bounds):
        job = Job(None, JobKind.control_layer, JobParams(bounds, f"[Control] {control.mode.text}"))
        job.control = control
        return self.add_job(job)

    def add_upscale(self, bounds: Bounds, seed: int):
        name = f"[Upscale] {bounds.width}x{bounds.height}"
        job = Job(None, JobKind.upscaling, JobParams(bounds, name, seed=seed))
        return self.add_job(job)

    def add_job(self, job: Job):
        self._entries.append(job)
        self.count_changed.emit()
        return job

    def remove(self, job: Job):
        # Diffusion jobs: kept for history, pruned according to meomry usage
        # Control layer jobs: removed immediately once finished
        self._entries.remove(job)
        self.count_changed.emit()

    def find(self, id: str):
        return next((j for j in self._entries if j.id == id), None)

    def count(self, state: JobState):
        return sum(1 for j in self._entries if j.state is state)

    def set_results(self, job: Job, results: ImageCollection):
        job.results = results
        if job.kind is JobKind.diffusion:
            self._memory_usage += results.size / (1024**2)
            self.prune(keep=job)

    def notify_started(self, job: Job):
        job.state = JobState.executing
        self.count_changed.emit()

    def notify_finished(self, job: Job):
        job.state = JobState.finished
        self.job_finished.emit(job)
        self.count_changed.emit()

    def notify_cancelled(self, job: Job):
        job.state = JobState.cancelled
        self.count_changed.emit()

    def notify_used(self, job_id: str, index: int):
        job = ensure(self.find(job_id))
        job._in_use[index] = True
        self.result_used.emit(self.Item(job_id, index))

    def prune(self, keep: Job):
        while self._memory_usage > settings.history_size and self._entries[0] != keep:
            discarded = self._entries.popleft()
            self._memory_usage -= discarded.results.size / (1024**2)
            self.job_discarded.emit(discarded)

    def select(self, job_id: str, index: int):
        self.selection = self.Item(job_id, index)

    def any_executing(self):
        return any(j.state is JobState.executing for j in self._entries)

    def __len__(self):
        return len(self._entries)

    def __getitem__(self, i):
        return self._entries[i]

    def __iter__(self):
        return iter(self._entries)

    @property
    def selection(self):
        return self._selection

    @selection.setter
    def selection(self, value: Item | None):
        if self._selection != value:
            self._selection = value
            self.selection_changed.emit()

    @property
    def memory_usage(self):
        return self._memory_usage
