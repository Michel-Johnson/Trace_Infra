export type DuplicateImportState = 'imported' | 'submitted';

export function duplicateImportFromCode(code: string | undefined): DuplicateImportState | null {
  if (code === 'UPLOAD_ALREADY_IMPORTED') return 'imported';
  if (code === 'UPLOAD_ALREADY_SUBMITTED') return 'submitted';
  return null;
}

export function reusedSuccessfulImport(reused: boolean | undefined, taskState: string): boolean {
  return reused === true && taskState === 'succeeded';
}

export function duplicateImportNotice(state: DuplicateImportState): string {
  return state === 'imported'
    ? '相同 Trace 已导入，本次未重复导入。'
    : '已有相同文件的上传记录，尚不能确认导入成功；请到轨迹库核对。';
}
