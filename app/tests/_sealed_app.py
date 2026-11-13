"""운영 코드가 선언한 봉인 대상 그대로 시험용 격리 앱 트리를 구성한다."""
from pathlib import Path
import shutil


def copy_implementation_tree(source_root, destination_root):
    import asis_runtime
    source_root = Path(source_root).resolve()
    destination_root = Path(destination_root).resolve()
    copied = []
    for source in asis_runtime.implementation_files(source_root):
        if not source.is_file():
            raise FileNotFoundError(str(source))
        relative = source.relative_to(source_root)
        target = destination_root/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative.as_posix())
    return copied
