// Uses an already installed Playwright; all served assets and evidence stay in this project.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../win/live2d'),out=path.resolve(__dirname,'../.private/evidence/v5-browser');
(async()=>{
  assert.ok(fs.existsSync(path.join(root,'index.html')),'step 2: transparent Live2D page must exist');
  fs.mkdirSync(out,{recursive:true});
  const server=http.createServer((req,res)=>{
    const file=path.join(root,decodeURIComponent(new URL(req.url,'http://localhost').pathname));
    if(!file.startsWith(root+path.sep)){res.writeHead(403).end();return;}
    fs.readFile(file.endsWith(path.sep)?file+'index.html':file,(err,data)=>{
      if(err){res.writeHead(404).end();return;}
      const ext=path.extname(file);res.setHeader('Content-Type',({'.css':'text/css','.js':'text/javascript','.html':'text/html','.json':'application/json','.png':'image/png'})[ext]||'application/octet-stream');res.end(data);
    });
  });
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  let browser;
  try{
    browser=await chromium.launch({headless:true,args:['--autoplay-policy=no-user-gesture-required','--use-angle=swiftshader','--enable-unsafe-swiftshader'],executablePath:process.env.DESK_TEST_CHROMIUM||undefined});
    const context=await browser.newContext({viewport:{width:420,height:620},recordVideo:{dir:out,size:{width:420,height:620}}});
    const page=await context.newPage(),errors=[],requests=[];
    page.on('pageerror',e=>errors.push(String(e)));page.on('console',m=>console.log('BROWSER',m.type(),m.text()));page.on('request',r=>requests.push(r.url()));
    const videoEpoch=Date.now();
    const response=await page.goto(`http://127.0.0.1:${server.address().port}/index.html`);assert.equal(response.status(),200);
    await page.waitForFunction(()=>window.deskCompanion?.ready,{timeout:30000});
    assert.equal(await page.evaluate(()=>getComputedStyle(document.body).backgroundColor),'rgba(0, 0, 0, 0)');
    await page.screenshot({path:path.join(out,'loaded.png'),omitBackground:true});
    console.log('GL',await page.evaluate(()=>{let c=document.querySelector('canvas'),g=c.getContext('webgl');return {size:[c.width,c.height],max:g.getParameter(g.MAX_TEXTURE_SIZE),err:g.getError(),lost:g.isContextLost(),rect:c.getBoundingClientRect().toJSON()}}));
    console.log('alpha grid',await page.evaluate(()=>Array.from({length:8},(_,i)=>Array.from({length:7},(_,j)=>deskCompanion.hit((j+1)*420/8,(i+1)*620/9)))));
    const alpha=await page.evaluate(()=>({outside:deskCompanion.hit(3,3),body:deskCompanion.hit(innerWidth/2,innerHeight*.65)}));
    assert.equal(alpha.outside,false);assert.equal(alpha.body,true,'character is draggable, blank pixels are not');
    const audio=fs.readFileSync(path.resolve(__dirname,'../.private/evidence/v5-mock.mp3')).toString('base64');
    const text='【mock 验收音频】字幕在他头顶，嘴型跟随这段测试音量。长句自动换行，透明区域仍可点击后面的游戏。';
    const audioOffset=(Date.now()-videoEpoch)/1000;
    await page.evaluate(({text,audio})=>deskCompanion.receive({type:'say',text,audio}),{text,audio});
    const samples=[];
    for(let i=0;i<40;i++){await page.waitForTimeout(125);samples.push(await page.evaluate(()=>deskCompanion.mouth()));}
    assert.ok(Math.max(...samples)>.15,'actual mock audio opens mouth');
    assert.ok(samples.some(v=>v<.02),'envelope has silent gaps');
    await page.waitForFunction(()=>deskCompanion.mouth()<.001,{timeout:6000});
    const bounds=await page.locator('#subtitle').boundingBox();assert.ok(bounds.x>=0&&bounds.y>=0&&bounds.x+bounds.width<=420&&bounds.y+bounds.height<=620);
    await page.screenshot({path:path.join(out,'subtitle.png'),omitBackground:true});
    await page.waitForFunction(()=>Number(getComputedStyle(document.querySelector('#subtitle')).opacity)===0,{timeout:12000});
    const longText='这是完整的长字幕。'.repeat(14).slice(0,120);
    await page.evaluate(text=>deskCompanion.receive({type:'say',text}),longText);
    await page.waitForTimeout(100);
    assert.equal(await page.locator('#subtitle').textContent(),longText);
    assert.ok(await page.locator('#subtitle').evaluate(e=>e.scrollHeight<=e.clientHeight),'120-character production reply must not be clipped');
    await page.evaluate(()=>deskCompanion.receive({type:'status',text:'在听…'}));
    const subtitleBounds=await page.locator('#subtitle').boundingBox(),statusBounds=await page.locator('#status').boundingBox();
    assert.ok(subtitleBounds.y+subtitleBounds.height<=statusBounds.y,'listening status must not overlap long subtitles');
    await page.screenshot({path:path.join(out,'subtitle-120.png'),omitBackground:true});
    await page.evaluate(()=>deskCompanion.receive({type:'status',text:'在听…'}));
    assert.equal(await page.locator('#status').textContent(),'在听…');
    await page.evaluate(()=>deskCompanion.receive({type:'say',text:'【mock】无效音频仍显示字幕',audio:'aW52YWxpZA=='}));
    await page.waitForTimeout(500);assert.equal(await page.evaluate(()=>deskCompanion.mouth()),0);
    console.log('request origins',requests.map(u=>new URL(u).origin));
    assert.ok(!requests.some(u=>u.includes(':8192')||(!u.startsWith('http://127.0.0.1:')&&!u.startsWith('blob:http://127.0.0.1:'))),'no phone or external requests');
    assert.deepEqual(errors,[]);
    const video=page.video();await context.close();await video.saveAs(path.join(out,'live2d.webm'));
    fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({pass:true,alpha,mouthMax:Math.max(...samples),samples,audioOffset,requests:requests.length,errors},null,2));
    console.log('PASS: r4d render, alpha hit, audio envelope, end-close, head subtitle/wrap/fade, status, invalid audio; no external requests');
  }finally{if(browser)await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);process.exitCode=1;});
