#!/usr/bin/env python3
"""Build MegaTransfer Desktop as a standalone .exe using PyInstaller."""
import subprocess
import sys
import os
import shutil

def main():
    # Install dependencies
    print("Installing dependencies...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
    
    # Check if psiphon binary exists
    psiphon_path = os.path.join(os.path.dirname(__file__), "psiphon-tunnel-core.exe")
    add_data = []
    if os.path.exists(psiphon_path):
        add_data.append(f"--add-data={psiphon_path};.")
        print(f"Bundling Psiphon from: {psiphon_path}")
    else:
        print("WARNING: psiphon-tunnel-core.exe not found. IP rotation will not work.")
        print("Download it from: https://github.com/nickoala/nickoala-psiphon-tunnel-core-builds")
    
    # Build with PyInstaller
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--windowed",
        "--name", "MegaTransfer",
        "--icon", "icon.ico" if os.path.exists("icon.ico") else "NONE",
        "--clean",
        "--noconfirm",
    ] + add_data + [
        "mega_transfer.py"
    ]
    
    print(f"Building: {' '.join(cmd)}")
    subprocess.check_call(cmd)
    
    # Copy output
    dist_exe = os.path.join("dist", "MegaTransfer.exe")
    if os.path.exists(dist_exe):
        size = os.path.getsize(dist_exe) / 1024 / 1024
        print(f"\nBuild complete! Output: {dist_exe} ({size:.1f} MB)")
        print("You can distribute this single .exe file.")
    else:
        print("Build failed - no output file found")
        sys.exit(1)

if __name__ == "__main__":
    main()
