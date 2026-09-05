"""Run after installation: python tools/revision_demo.py. No model needed."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from noveltool.db import ProjectStore
from noveltool.runtime import ProjectSession


async def main(path):
    with ProjectStore.create(path, '正文引擎演示'):
        pass
    session = ProjectSession(ProjectStore.open(path))
    try:
        original = '　第一段😀。\n\n第二段。\n'
        await session.import_manuscript(original, 0)
        print('导入：', repr(session.manuscript.text))
        await session.replace_manuscript(1, 4, '改写后的文字', 1)
        print('替换：', repr(session.manuscript.text))
        await session.undo_manuscript(2)
        print('撤销：', repr(session.manuscript.text))
        assert session.manuscript.text == original
        print('逐字恢复成功；revision_no =', session.manuscript.revision_no)
    finally:
        await session.close()


if __name__ == '__main__':
    with TemporaryDirectory() as root:
        asyncio.run(main(Path(root) / 'demo.sqlite3'))
