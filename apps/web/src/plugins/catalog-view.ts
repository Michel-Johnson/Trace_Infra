import type {Extension} from '../api/extensions';

export type PluginGroup = {id:string;latest:Extension;versions:Extension[]};
export const extensionVersionKey = (entry:Extension) => JSON.stringify([entry.source,entry.manifest.plugin_id,entry.manifest.version]);

/** Semver precedence for release versions; opaque legacy version IDs have a stable natural order. */
export function comparePluginVersions(a:string,b:string):number {
  const parse=(value:string)=>/^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([\w.-]+))?(?:\+[\w.-]+)?$/.exec(value);
  const av=parse(a),bv=parse(b);
  if(!av||!bv)return a.localeCompare(b,undefined,{numeric:true});
  for(let i=1;i<=3;i++){const difference=Number(av[i]||0)-Number(bv[i]||0);if(difference)return difference;}
  if(!av[4]||!bv[4])return av[4]? -1:bv[4]?1:0;
  const ap=av[4].split('.'),bp=bv[4].split('.');
  for(let i=0;i<Math.max(ap.length,bp.length);i++){
    if(ap[i]===undefined)return -1;if(bp[i]===undefined)return 1;
    if(ap[i]===bp[i])continue;
    const an=/^\d+$/.test(ap[i]),bn=/^\d+$/.test(bp[i]);
    return an&&bn?Number(ap[i])-Number(bp[i]):an?-1:bn?1:ap[i].localeCompare(bp[i]);
  }
  return 0;
}

export function groupPluginCatalog(items:Extension[]):PluginGroup[] {
  const groups=new Map<string,Extension[]>();
  for(const entry of items){const id=entry.manifest.plugin_id;groups.set(id,[...(groups.get(id)||[]),entry]);}
  return [...groups].map(([id,versions])=>{
    const sorted=[...versions].sort((a,b)=>comparePluginVersions(b.manifest.version,a.manifest.version)
      ||Number(b.source==='native')-Number(a.source==='native')||a.source.localeCompare(b.source));
    return {id,latest:sorted[0],versions:sorted};
  });
}

export function pluginMatches(group:PluginGroup,kind:string,search:string):boolean {
  if(kind!=='all'&&!group.latest.manifest.contributes.some(c=>c.kind===kind))return false;
  const query=search.trim().toLocaleLowerCase();
  return !query||[group.id,group.latest.manifest.title,group.latest.manifest.description].some(value=>value.toLocaleLowerCase().includes(query));
}

export function shortPluginPurpose(description:string):string {
  const sentence=description.trim().split(/[。；\n]/)[0];
  return sentence.length>64?sentence.slice(0,63)+'…':sentence;
}
