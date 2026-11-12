// Fail before npm can accidentally fall back to an unprepared global tsc.
const fs=require('node:fs');
const path=require('node:path');
for (const directory of ['contracts/generated/typescript','clients/web']) {
  const root=path.join(process.argv[2],directory);
  const manifest=JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8'));
  const declared=manifest.devDependencies.typescript;
  const installed=JSON.parse(fs.readFileSync(path.join(root,'node_modules/typescript/package.json'),'utf8')).version;
  if (!/^7\.\d+\.\d+$/.test(declared) || installed!==declared) throw new Error(directory+': install pinned TypeScript 7 with npm ci');
  console.log(directory+': TypeScript '+installed);
}
