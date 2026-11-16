"""Build the Windows x64 payload from the working INTELLIVIZ environment."""

import importlib.metadata as metadata
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile, ZIP_DEFLATED

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


OUTPUT = Path(__file__).resolve().parent
APP = OUTPUT.parent


def distributions():
    pending = [Requirement(line) for line in (APP / 'requirements.txt').read_text().splitlines()
               if line and not line.startswith('--')]
    seen = set()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        if name in seen or (requirement.marker and not requirement.marker.evaluate()):
            continue
        distribution = metadata.distribution(name)
        version = distribution.version
        # The torchvision CPU wheel can expose its build suffix only at runtime.
        if name == 'torchvision' and not requirement.specifier.contains(version):
            from torchvision import __version__
            version = __version__
        if not requirement.specifier.contains(version):
            raise RuntimeError(f'{requirement} required, found {version}.')
        seen.add(name)
        pending.extend(Requirement(entry) for entry in distribution.requires or [])
        yield distribution


def write_tree(bundle, folder, destination):
    for path in folder.rglob('*'):
        relative = path.relative_to(folder)
        if (path.is_file() and not {'__pycache__', 'site-packages', 'test'}.intersection(relative.parts)
                and path.suffix != '.pyc'):
            bundle.write(path, Path(destination) / relative)


def build():
    runtime = Path(sys.base_prefix)
    archive = OUTPUT / 'INTELLIVIZ-Windows-x64.zip'
    draft = archive.with_suffix('.tmp')
    try:
        with ZipFile(draft, 'w', ZIP_DEFLATED, compresslevel=1) as bundle:
            for path in [runtime / 'python.exe', runtime / 'LICENSE.txt', *runtime.glob('*.dll')]:
                bundle.write(path, Path('runtime') / path.name)
            bundle.writestr(f'runtime/python{sys.version_info.major}{sys.version_info.minor}._pth',
                            '.\nDLLs\nLib\nLib/site-packages\n..\nimport site\n')
            for folder in ('Lib', 'DLLs', 'tcl'):
                write_tree(bundle, runtime / folder, f'runtime/{folder}')
            for distribution in distributions():
                print(f'Bundling {distribution.metadata["Name"]}', flush=True)
                for entry in distribution.files or []:
                    if '..' in entry.parts or entry.suffix == '.pyc':
                        continue
                    path = entry.locate()
                    if path.is_file():
                        bundle.write(path, Path('runtime/Lib/site-packages') / entry)
            ffmpeg = Path(shutil.which('ffmpeg') or '')
            bundle.write(ffmpeg, 'runtime/ffmpeg.exe')
            for name in ('LICENSE', 'README.txt'):
                notice = ffmpeg.parent.parent / name
                if notice.is_file():
                    bundle.write(notice, f'runtime/ffmpeg-{name}')
            for path in [*APP.glob('*.py'), APP / 'Start-INTELLIVIZ.ps1',
                         APP / '.streamlit/config.toml']:
                bundle.write(path, path.relative_to(APP))
            for folder in ('assets', 'models'):
                write_tree(bundle, APP / folder, folder)
        draft.replace(archive)
        print(archive, flush=True)
    finally:
        draft.unlink(missing_ok=True)


if __name__ == '__main__':
    build()
