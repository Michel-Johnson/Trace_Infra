// Export unmodified Ant Design icon geometry from the existing locked dependency.
import {createRequire} from 'node:module';
import {mkdir, writeFile, readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {dirname, resolve} from 'node:path';
const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(resolve(root, 'apps/web/package.json'));
const target = resolve(root, 'apps/web/public/guide-icons');
const icons = {search:'SearchOutlined',arrow:'ArrowRightOutlined',down:'DownOutlined',right:'RightOutlined',file:'FileTextOutlined',database:'DatabaseOutlined',link:'LinkOutlined',api:'ApiOutlined',close:'CloseOutlined',check:'CheckOutlined',export:'ExportOutlined',layout:'AppstoreOutlined',code:'CodeOutlined',read:'ReadOutlined',write:'EditOutlined',user:'UserOutlined',model:'RobotOutlined'};
const escape = value => String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;');
const render = ({tag, attrs = {}, children = []}) => `<${tag}${Object.entries(attrs).map(([key,value]) => ` ${key}="${escape(value)}"`).join('')}>${children.map(render).join('')}</${tag}>`;
await mkdir(target, {recursive:true});
for (const [name, definition] of Object.entries(icons)) {
  const {icon} = require(`@ant-design/icons-svg/lib/asn/${definition}.js`).default;
  const svg = {...icon, attrs:{...icon.attrs, xmlns:'http://www.w3.org/2000/svg', width:'24', height:'24', fill:'#53616e'}};
  await writeFile(resolve(target, `${name}.svg`), render(svg));
}
const license = resolve(root, 'docs/licenses/ant-design-icons.txt');
await writeFile(resolve(target, 'LICENSE'), await readFile(license));
