"""apps controllers"""

from .kubernetes import K8sController
from .clabernetes import C9sController
from .git import GitController

class _LazyProxy:
    def __init__(self, factory):
        self._factory = factory
        self._inst = None
    def _get(self):
        if self._inst is None:
            self._inst = self._factory()
        return self._inst
    def __getattr__(self, name):
        return getattr(self._get(), name)
    def __call__(self, *args, **kwargs):
        return self._get()

# lazy proxies are only instantiated when someone uses it
k8s = _LazyProxy(lambda: K8sController())
c9s = _LazyProxy(lambda: C9sController())
git = _LazyProxy(lambda: GitController())

__all__ = ["K8sController", "C9sController", "k8s", "c9s", "git"]
