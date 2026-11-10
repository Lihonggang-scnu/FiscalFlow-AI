"""Build the allowlisted enterprise internal source delivery without runtime data."""
from __future__ import annotations
import argparse
import hashlib
import json
import zipfile
from pathlib import Path

PROJECT=Path(__file__).resolve().parents[1]
FILES=[
 ('docs/企业版内部交付_开始使用.md','README.md'),
 ('requirements.txt','requirements.txt'),('pyproject.toml','pyproject.toml'),
 ('config/enterprise_budget.example.json','config/enterprise_budget.example.json'),
 ('examples/enterprise_invoices.csv','examples/enterprise_invoices.csv'),
 ('scripts/enterprise_app.py','scripts/enterprise_app.py'),
 ('scripts/start_enterprise.ps1','scripts/start_enterprise.ps1'),
 ('scripts/stop_enterprise.ps1','scripts/stop_enterprise.ps1'),
 ('scripts/enterprise_admin.py','scripts/enterprise_admin.py'),
 ('scripts/build_internal_delivery.py','scripts/build_internal_delivery.py'),
 ('docs/企业工作台运行说明_2026-10-27.md','docs/企业工作台运行说明_2026-10-27.md'),
 ('docs/企业版内部交付验收报告_2026-10-27.md','docs/企业版内部交付验收报告_2026-10-27.md'),
 ('docs/企业产品验收矩阵_2026-10-27.csv','docs/企业产品验收矩阵_2026-10-27.csv'),
 ('tests/test_enterprise_workflow.py','tests/test_enterprise_workflow.py'),
 ('tests/test_enterprise_web.py','tests/test_enterprise_web.py'),
 ('artifacts/enterprise_delivery_20261027/internal_evidence_summary.json','acceptance/summary.json'),
 ('artifacts/enterprise_delivery_20261027/browser_test/ui_export.csv','acceptance/ui_export.csv'),
]

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=PROJECT/'dist/TaxAgentHarness_Enterprise_Internal_20261027.zip');args=parser.parse_args()
    if args.output.exists():raise SystemExit('Output already exists; choose a fresh output name.')
    files=list(FILES)
    for p in sorted((PROJECT/'src').rglob('*.py')):
        if '__pycache__' not in p.parts:files.append((p.relative_to(PROJECT).as_posix(),p.relative_to(PROJECT).as_posix()))
    for p in sorted((PROJECT/'src/taxharness/enterprise/static').glob('*.html')):files.append((p.relative_to(PROJECT).as_posix(),p.relative_to(PROJECT).as_posix()))
    manifest=[]
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(args.output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as package:
        for local,archive in sorted(files,key=lambda x:x[1]):
            p=PROJECT/local
            if not p.is_file():p=PROJECT/archive
            data=p.read_bytes()
            package.writestr(archive,data);manifest.append({'path':archive,'source':local,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        package.writestr('manifest.json',json.dumps({'kind':'enterprise internal source delivery','files':manifest,'runtime_data_included':False,'video_included':False},ensure_ascii=False,indent=2))
    with zipfile.ZipFile(args.output) as package:assert package.testzip() is None
    result={'path':str(args.output),'bytes':args.output.stat().st_size,'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),'files':len(manifest)+1}
    metadata=PROJECT/'artifacts/enterprise_delivery_20261027/internal_package.json'
    metadata.parent.mkdir(parents=True,exist_ok=True)
    metadata.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
