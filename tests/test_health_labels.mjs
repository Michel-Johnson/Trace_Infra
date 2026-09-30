import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';

const source=await readFile(new URL('../apps/web/src/components/InfraPages.tsx',import.meta.url),'utf8');
const styles=await readFile(new URL('../apps/web/src/workspace.css',import.meta.url),'utf8');

test('storage health page presents all user-facing metrics in Chinese',()=>{
  for(const label of ['数据库','搜索后端','Trigram 文本索引','轨迹','事件','搜索文档','时间信息','状态信息','上下文','Token','对象统计','全部对象','消息','工具调用','关系','索引状态','采集覆盖率']) assert.match(source,new RegExp(label));
  assert.doesNotMatch(source,/模糊搜索索引/);
  assert.doesNotMatch(source,/令牌用量/);
  for(const label of ['已完成','未索引','失败','模型请求','完整','部分','缺失','未知','已启用','未启用']) assert.match(source,new RegExp(label));
  for(const label of ['label="Database"','label="Search Backend"','label="Trigram"','label="Trace"','label="Span"','<header>Projection</header>','<header>Capture Coverage</header>']) assert.doesNotMatch(source,new RegExp(label));
});

test('capture coverage uses one shared header and fits the three-card row',()=>{
  assert.match(source,/className="th-health-coverage-head"/);
  assert.match(source,/role="columnheader"/);
  assert.match(source,/className="th-health-coverage-row"/);
  assert.doesNotMatch(source,/className="th-coverage-values"/);
  assert.doesNotMatch(styles,/\.th-health-facts>\.th-health-coverage\{grid-column:1\/-1\}/);
  assert.match(styles,/grid-template-columns:minmax\(72px,1\.35fr\) repeat\(4,minmax\(28px,\.55fr\)\)/);
});
