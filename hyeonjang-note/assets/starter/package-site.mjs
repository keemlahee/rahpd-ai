import {spawnSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {resolve} from 'node:path';
const archive=process.argv[2];
if(!archive)throw new Error('Usage: node package-site.mjs <absolute archive.tar>');
for(const file of ['.openai/hosting.json','dist/server/index.js','drizzle/meta/_journal.json'])if(!existsSync(file))throw new Error('Missing build input: '+file);
// Sites discovers the Worker at dist/server/index.js and D1 migrations at drizzle/.
// Keep a single migration tree at the archive root.
const result=spawnSync('tar',['-cf',resolve(archive),'.openai','dist/server','drizzle'],{stdio:'inherit',windowsHide:true});
if(result.status!==0)throw new Error('Unable to package Site');
console.log('Packaged Worker, hosting manifest, and root database migrations.');
