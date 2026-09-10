/* 可选浏览器验收：需要 Playwright；服务和数据库在独立临时目录中创建。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.MINISQL_PLAYWRIGHT || 'playwright');
const root = path.resolve(__dirname, '../..');
fs.mkdirSync(path.join(root, 'data'), {recursive:true});
const directory = fs.mkdtempSync(path.join(root, 'data/gui-browser-'));
const python = process.env.MINISQL_PYTHON || path.join(root, '.venv', 'Scripts', 'python.exe');
const service = spawn(python, ['-X', 'utf8', '-m', 'minisql.gui', '--data-dir', directory, '--port', '0', '--no-browser'],
  {cwd:root, env:{...process.env, PYTHONPATH:path.join(root,'src')}, windowsHide:true});
let browser;
const errors = [];
const originReady = new Promise((resolve,reject) => {
  let output = '';
  const timer = setTimeout(() => reject(new Error('GUI 服务启动超时')), 15000);
  service.stdout.on('data', chunk => { output += chunk; const match = output.match(/http:\/\/127\.0\.0\.1:\d+/); if(match){ clearTimeout(timer); resolve(match[0]); } });
  service.on('error', reject);
  service.on('exit', code => { clearTimeout(timer); if(!output.includes('http:')) reject(new Error('服务启动失败 '+code)); });
  service.stderr.on('data', chunk => process.stderr.write(chunk));
});
(async()=>{
  const origin = await originReady;
  browser = await chromium.launch({headless:true, ...(process.env.MINISQL_BROWSER ? {executablePath:process.env.MINISQL_BROWSER} : {})});
  const context = await browser.newContext({viewport:{width:1440,height:1000},acceptDownloads:true});
  const page = await context.newPage();
  page.on('pageerror', error=>errors.push(error.message));
  page.on('console', message=>{if(message.type()==='error')errors.push(message.text());});
  page.on('dialog', dialog=>dialog.accept());
  await page.goto(origin); await page.waitForLoadState('networkidle');
  await page.waitForFunction(()=>document.querySelector('#connection-status').textContent==='已连接');
  assert.equal(await page.locator('#table-count').innerText(),'0');
  async function settle(){await page.waitForFunction(()=>!document.querySelector('#run-all').disabled);}
  async function run(sql, ok=true){
    await page.locator('#sql-editor').fill(sql);
    const reply = page.waitForResponse(response=>response.url()===origin+'/api/execute');
    await page.locator('#run-all').click(); const data = await (await reply).json(); await settle();
    assert.equal(data.ok,ok,JSON.stringify(data.error)); return data;
  }
  const sample = await page.locator('#sql-editor').inputValue();
  const first = await run(sample); assert.equal(first.results.at(-1).rows.length,2);
  assert.equal(await page.locator('#table-count').innerText(),'1');
  await run('SELECT id, name, score FROM experiments\nWHERE score > 80 AND 1 = 1;');
  fs.mkdirSync(path.join(root,'docs/assets'),{recursive:true});
  await page.screenshot({path:path.join(root,'docs/assets/gui-workbench.png'),fullPage:true});
  await page.locator('[data-view="compiler"]').click();
  assert.equal(await page.locator('#trace-count').innerText(),'1');
  assert.match(await page.locator('#compiler-content').innerText(),/Token 流/);
  assert.match(await page.locator('#compiler-content').innerText(),/优化后/);
  await page.locator('[data-view="cache"]').click();
  assert.equal(await page.locator('.metric-value').count(),4);
  await page.screenshot({path:path.join(root,'docs/assets/gui-cache.png'),fullPage:true});
  await page.locator('[data-view="workbench"]').click();
  // 键盘执行与选区位置保留。
  await page.locator('#sql-editor').fill('SELECT * FROM experiments;');
  const keyboard = page.waitForResponse(r=>r.url()===origin+'/api/execute');
  await page.locator('#sql-editor').press('Control+Enter'); assert.equal((await (await keyboard).json()).ok,true); await settle();
  await page.locator('#sql-editor').fill('DROP TABLE experiments;\nSELECT * FROM experiments;');
  await page.locator('#sql-editor').evaluate(node=>{node.focus();node.setSelectionRange(node.value.indexOf('SELECT'),node.value.length);node.dispatchEvent(new Event('select'));});
  const selected=page.waitForResponse(r=>r.url()===origin+'/api/execute');
  await page.locator('#run-selection').click(); assert.equal((await (await selected).json()).results[0].rows.length,3); await settle();
  const bad = await run('-- test\nSELECT absent FROM experiments;',false); assert.equal(bad.error.position.line,2);
  await page.getByRole('button',{name:/定位到第 2 行/}).click();
  assert.match(await page.locator('#cursor-position').innerText(),/行 2/);
  // 显式事务由多个请求连续操作，错误后必须回滚。
  await page.locator('#begin').click(); await page.waitForFunction(()=>document.querySelector('#transaction-status').textContent.includes('进行中'));
  await run('DELETE FROM experiments WHERE id=1;');
  await run("INSERT INTO experiments(id,name,score) VALUES ('bad','x',1);",false);
  assert.equal(await page.locator('#commit').isDisabled(),true);
  await page.locator('#rollback').click(); await settle();
  assert.equal((await run('SELECT * FROM experiments;')).results[0].rows.length,3);
  // 多语句错误不重复执行或伪装为整体回滚。
  const partial = await run("INSERT INTO experiments(id,name,score) VALUES (4,'Once',88); SELECT missing FROM experiments;",false);
  assert.deepEqual(partial.results,[]);
  assert.equal((await run('SELECT * FROM experiments WHERE id=4;')).results[0].rows.length,1);
  // 结果分页不会再次发送 SQL。
  const inserts=Array.from({length:55},(_,i)=>`INSERT INTO experiments(id,name,score) VALUES (${i+10},'row',50);`).join('\n');
  await run(inserts); await run('SELECT * FROM experiments;');
  assert.equal(await page.locator('#results tbody tr').count(),50);
  let requests=0; const count=r=>{if(r.url().endsWith('/api/execute'))requests++;}; page.on('request',count);
  await page.getByRole('button',{name:'下一页',exact:true}).click();
  assert.equal(await page.locator('#results tbody tr').count(),9); assert.equal(requests,0); page.off('request',count);
  // 数据按文本展示，不解析 HTML。
  await run("INSERT INTO experiments(id,name,score) VALUES (100,'<img src=x onerror=alert(1)>',1); SELECT name FROM experiments WHERE id=100;");
  assert.equal(await page.locator('#results img').count(),0);
  assert.match(await page.locator('#results').innerText(),/<img src=x/);
  // 文件打开、保存，中文内容无损。
  const fileSql="SELECT name FROM experiments; -- 中文文件\n";
  await page.locator('#file-input').setInputFiles({name:'实验.sql',mimeType:'text/plain',buffer:Buffer.from(fileSql)});
  await page.waitForFunction(()=>document.querySelector('#file-name').textContent==='实验.sql');
  assert.equal(await page.locator('#sql-editor').inputValue(),fileSql);
  const download=page.waitForEvent('download'); await page.locator('#save-file').click(); const saved=await download;
  const stream=await saved.createReadStream(); const chunks=[]; for await(const chunk of stream)chunks.push(chunk);
  assert.equal(Buffer.concat(chunks).toString('utf8'),fileSql);
  // 多页面隔离及断开回滚。
  const second=await context.newPage(); second.on('dialog',d=>d.accept());
  await second.goto(origin); await second.waitForFunction(()=>document.querySelector('#connection-status').textContent==='已连接');
  await run('BEGIN; DELETE FROM experiments WHERE id=1;');
  await second.locator('#sql-editor').fill('SELECT * FROM experiments;');
  const busy=second.waitForResponse(r=>r.url()===origin+'/api/execute'); await second.locator('#run-all').click();
  assert.equal((await (await busy).json()).error.code,'DATABASE_BUSY');
  await page.locator('#connection-button').click(); await page.locator('#disconnect').click();
  await page.waitForFunction(()=>document.querySelector('#connection-status').textContent==='已断开');
  await page.locator('#connection-button').click(); await page.locator('#connect').click(); await settle();
  assert.equal((await run('SELECT * FROM experiments WHERE id=1;')).results[0].rows.length,1);
  await second.close();
  // 切换到另一个目录，旧库仍保留。
  await page.locator('#connection-button').click(); await page.locator('#directory-input').fill(path.join(directory,'another'));
  await page.locator('#connect').click(); await settle(); assert.equal(await page.locator('#table-count').innerText(),'0');
  await page.locator('#connection-button').click(); await page.locator('#directory-input').fill(directory); await page.locator('#connect').click(); await settle();
  assert.equal(await page.locator('#table-count').innerText(),'1');
  // 窄屏不产生页面级横向溢出。
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
  await page.screenshot({path:path.join(directory,'mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);
  await page.locator('#connection-button').click(); await page.locator('#disconnect').click();
  await page.waitForFunction(()=>document.querySelector('#connection-status').textContent==='已断开');
  await browser.close(); browser=null;
  console.log('Browser checks passed: layout, SQL, keyboard, selection, trace, cache, transactions, errors, pagination, files, escaping, sessions, directory switch, mobile.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  service.kill();
});
