from pathlib import Path
import subprocess


def test_frontend_limits_match_backend_boundaries():
    script=r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const code=fs.readFileSync('static/product-import.js','utf8');
const ctx={};vm.createContext(ctx);
vm.runInContext(code.slice(code.indexOf('function importFilesAllowed('),code.indexOf('async function request(')),ctx);
const check=ctx.importFilesAllowed;
assert(check(Array.from({length:20000},()=>({size:0}))));
assert(!check(Array.from({length:20001},()=>({size:0}))));
assert(check([{size:1024**3}]));
assert(!check([{size:1024**3+1}]));
assert(!check([{size:1024**3},{size:1}]));
assert(!check([]));assert(!check([{size:-1}]));
"""
    r=subprocess.run(['node','-e',script],cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
