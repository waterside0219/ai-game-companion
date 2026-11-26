// 影之刃零 PBZSaveGame.sav 只读解码。AES-256-GCM 的密钥是全体玩家共用的一把，存档编辑社区公开整理过；
// 这里不放，运行前设 PBZ_SAVE_KEY=<64 位十六进制>。
// 目录/槽结构照 Duucking/pbz-save-editing-guide (MIT) 的 pbz_dump.js；Kraken 解压要一个编成 wasm 的 ooz，放在 ./wasm/ooz.mjs。
// 用法: node pbz_read.mjs <PBZSaveGame.sav> <outDir>
import fs from 'node:fs'; import path from 'node:path'; import crypto from 'node:crypto'; import zlib from 'node:zlib';
import createOoz from './wasm/ooz.mjs';
if (!/^[0-9a-fA-F]{64}$/.test(process.env.PBZ_SAVE_KEY || '')) { console.error('set PBZ_SAVE_KEY (64 hex chars)'); process.exit(2); }
const KEY = Buffer.from(process.env.PBZ_SAVE_KEY,'hex');
const [,, savePath, outDir] = process.argv;
const save = fs.readFileSync(savePath);
const open = (off,len) => { const d=crypto.createDecipheriv('aes-256-gcm',KEY,save.subarray(off+8,off+20)); d.setAuthTag(save.subarray(off+20,off+36)); return Buffer.concat([d.update(save.subarray(off+36,off+len)),d.final()]); };
const version=save[4], count=save.readUInt16LE(6), dirSize=save.readUInt32LE(8);
const dir=open(12,dirSize); const base=12+dirSize; let p=12; const table=[];
const ooz = await createOoz({print:()=>{},printErr:()=>{}});
const kraken=(src,size)=>{const s=ooz._malloc(Math.max(1,src.length)), d=ooz._malloc(size+64); try{ooz.HEAPU8.set(src,s); const n=ooz._ooz_decompress(s,src.length,d,size); if(n<=0) throw new Error('ooz failed'); return Buffer.from(ooz.HEAPU8.slice(d,d+n));} finally{ooz._free(s);ooz._free(d);}};
fs.mkdirSync(outDir,{recursive:true});
for(let i=0;i<count;i++){
  const n=dir.readUInt16LE(p); p+=2; const name=dir.subarray(p,p+n).toString('utf8'); p+=n;
  const algo=dir[p]; p+=2; const rawSize=dir.readUInt32LE(p); p+=4; const offset=dir.readUInt32LE(p); p+=4; const blockSize=dir.readUInt32LE(p); p+=4;
  const plain=open(base+offset,blockSize);
  const raw = algo===0?plain: algo===1?zlib.inflateSync(plain): kraken(plain,rawSize);
  const fname=name.replace(/[^A-Za-z0-9_.-]/g,'_');
  fs.writeFileSync(path.join(outDir,fname+'.json'),raw);
  table.push({name,algo,rawSize,got:raw.length});
}
fs.writeFileSync(path.join(outDir,'_directory.json'),JSON.stringify({version,count,table},null,2));
console.log(JSON.stringify(table));
