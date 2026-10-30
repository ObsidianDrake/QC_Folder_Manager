from pathlib import Path

root = Path(SPECPATH)
a = Analysis([str(root / 'main.py')], pathex=[str(root)],
             binaries=[], datas=[(str(root/'qcmanager/static'), 'qcmanager/static'),
                                 (str(root/'qcmanager/fs_helper.py'), 'qcmanager')],
             hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='qc-folder-manager',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
