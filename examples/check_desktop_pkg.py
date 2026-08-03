import zipfile
z = zipfile.ZipFile(r'C:\Users\hslji\.mavis\sessions\mvs_934c9b1555d54a84b63a2ec145cd5ea4\workspace\novel_writer_20260604_151705.zip')
print('File count:', len(z.namelist()))
print()
key_files = ['desktop.py', 'novelai_desktop.spec', 'build.bat', 'requirements-desktop.txt', 'assets/icon.ico']
for kf in key_files:
    found = any(f.endswith(kf) for f in z.namelist())
    mark = "OK" if found else "MISSING"
    print(f"  {kf}: {mark}")
