/** Read every matching history page. A cancelled/failed read never returns a partial history. */
export async function readBatchHistory<T extends {id:string}>(
  fetchPage:(cursor:string|undefined,limit:number)=>Promise<T[]>,signal?:AbortSignal,
):Promise<T[]>{
  const result=new Map<string,T>(),cursors=new Set<string>();
  let cursor:string|undefined;
  const checkAbort=()=>{if(signal?.aborted)throw signal.reason||new DOMException('读取已取消','AbortError');};
  while(true){
    checkAbort();
    const page=await fetchPage(cursor,100);
    checkAbort();
    for(const batch of page)if(!result.has(batch.id))result.set(batch.id,batch);
    if(page.length<100)return [...result.values()];
    const following=page.at(-1)?.id;
    if(!following||cursors.has(following))throw new Error('批次历史游标未推进，请刷新重试');
    cursors.add(following);cursor=following;
  }
}
