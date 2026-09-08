import subprocess
from pathlib import Path


def test_knowledge_summary_preview_empty_and_advanced_are_read_only():
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync('static/app.js','utf8');
const elements={};const el=id=>elements[id]||(elements[id]={open:true});
const ctx={el};vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('function renderKnowledgeEditor('),source.indexOf('function openEdit(')),ctx);
const p={knowledge_source_path:'/library/45-demo/商品资料',knowledge_text:'只支持Mac。\n<script>never execute</script>',knowledge_chars:42,knowledge_file_count:2,knowledge_updated_at:'2026-09-08 10:00:00'};
const before=JSON.stringify(p);ctx.renderKnowledgeEditor(p);
assert.equal(JSON.stringify(p),before);
assert.equal(el('knowledgePreview').textContent,p.knowledge_text);
assert.equal(el('knowledgePreview').innerHTML,undefined);
assert(el('knowledgeStatus').textContent.includes('42'));
assert(el('knowledgeStatus').textContent.includes('2 个来源文件'));
assert(el('knowledgeSource').textContent.includes('已登记资料目录'));
assert(el('knowledgeUpdated').textContent.includes(p.knowledge_updated_at));
assert(!el('knowledgeAdvanced').open);assert(!el('knowledgePreviewDetails').open);
ctx.renderKnowledgeEditor({...p,knowledge_source_path:'C:\\legacy\\product'});
assert.equal(el('knowledgeFolderPath').value,'C:\\legacy\\product');
assert(!el('knowledgeSource').textContent.includes('导入商品包'));
ctx.renderKnowledgeEditor({...p,knowledge_source_path:'',knowledge_updated_at:null});
assert(el('knowledgeSource').textContent.includes('自动提取'));
assert(el('knowledgeUpdated').textContent.includes('尚无'));
ctx.renderKnowledgeEditor({knowledge_chars:999,knowledge_text:' '});
assert(el('knowledgeStatus').textContent.includes('暂无知识'));
assert(el('knowledgeStatus').textContent.includes('导入商品包'));
assert(el('knowledgePreview').textContent.includes('暂无'));
"""
    result = subprocess.run(['node', '-e', script], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_knowledge_controls_are_retained_inside_closed_advanced_section():
    html = (Path(__file__).resolve().parents[1] / 'static/index.html').read_text()
    section = html.split('<details id="knowledgeAdvanced">')[1].split('</details>')[0]
    for control in ['knowledgeFolderPath','pickKnowledgeFolder','loadKnowledgeFolder','clearKnowledgeFolder']:
        assert f'id="{control}"' in section
    assert '自动回复资料库' not in html
    assert '自动回复知识' in html
    assert 'id="knowledgePreview"' in html
    assert '不是Mac文件夹' in section
