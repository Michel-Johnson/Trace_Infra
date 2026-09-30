import test from 'node:test';
import assert from 'node:assert/strict';
import { duplicateImportFromCode, duplicateImportNotice, reusedSuccessfulImport } from '../src/lib/import-duplicate-status.ts';

test('only verified duplicate-import responses claim a Trace was imported', () => {
  assert.equal(duplicateImportFromCode('UPLOAD_ALREADY_IMPORTED'), 'imported');
  assert.equal(duplicateImportFromCode('UPLOAD_ALREADY_SUBMITTED'), 'submitted');
  assert.equal(duplicateImportFromCode('UNKNOWN_CONFLICT'), null);
  assert.match(duplicateImportNotice('imported'), /已导入.*未重复导入/);
  assert.match(duplicateImportNotice('submitted'), /尚不能确认导入成功/);
});

test('same-browser idempotent replay is not mistaken for a new import', () => {
  assert.equal(reusedSuccessfulImport(true, 'succeeded'), true);
  assert.equal(reusedSuccessfulImport(false, 'succeeded'), false);
  assert.equal(reusedSuccessfulImport(true, 'failed'), false);
  assert.equal(reusedSuccessfulImport(true, 'running'), false);
});
