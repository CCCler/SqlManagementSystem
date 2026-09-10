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
  await run("CREATE TABLE t(id INT, age INT, name VARCHAR); INSERT INTO t(id,age,name) VALUES (1,20,'甲');");
  const updated = await run("UPDATE t SET age=age+1, name='<img src=x onerror=alert(1)>' WHERE id=1;");
  assert.equal(updated.results[0].affected_rows,1);
  assert.match(await page.locator('#results').innerText(),/共影响 1 行/);
  assert.match(await page.locator('#highlight').innerText(),/UPDATE/);
  await page.locator('[data-view="compiler"]').click();
  assert.match(await page.locator('#compiler-content').innerText(),/Update/);
  await page.locator('[data-view="workbench"]').click();
  const query=await run('SELECT * FROM t;');
  assert.equal(query.results[0].rows[0][1],21);
  assert.equal(await page.locator('#results img').count(),0);
  assert.match(await page.locator('#results').innerText(),/onerror/);
  await run('BEGIN; UPDATE t SET age=99; ROLLBACK;');
  assert.equal((await run('SELECT * FROM t;')).results[0].rows[0][1],21);
  const bad=await run("UPDATE t SET age='bad';",false);
  assert.equal(bad.error.code,'TYPE_MISMATCH');
  assert.match(await page.locator('#results').innerText(),/TYPE_MISMATCH/);
  assert.deepEqual(errors,[]);
  await page.screenshot({path:path.join(directory,'update.png'),fullPage:true});
  console.log('UPDATE browser checks passed: affected rows, highlight, compilation, query, escaping, rollback, errors.');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  service.kill();
});
