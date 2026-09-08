// Isolated real-HTTP/browser smoke. No real providers, user DBs, credentials or .env changes.
import { createRequire } from "node:module";
import { spawn, execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createServer } from "node:net";
import assert from "node:assert/strict";
const require=createRequire(new URL("../web/package.json",import.meta.url));
const { chromium }=require("@playwright/test");
const adminRoot=fileURLToPath(new URL("../",import.meta.url));
const knowledgeRoot=resolve(adminRoot,"../Opsark_knowledge");
const directory=mkdtempSync(join(tmpdir(),"opsark-independent-"));
const password=randomBytes(24).toString("hex");
const children=[];
const python=root=>join(root,".venv",process.platform==="win32"?"Scripts/python.exe":"bin/python");
async function port(){const s=createServer();await new Promise(r=>s.listen(0,"127.0.0.1",r));const p=s.address().port;await new Promise(r=>s.close(r));return p;}
async function start(root,module,name){
 const p=await port(),base="http://127.0.0.1:"+p;
 const env={...process.env,DATABASE_URL:"sqlite:///"+join(directory,name+".db").replaceAll("\\","/"),TEST_PASSWORD:password,KNOWLEDGE_SERVICE_TOKEN:"",ALLOWED_ORIGINS:base,COOKIE_SECURE:"false",MODEL_KEY_ENCRYPTION_KEY:randomBytes(32).toString("base64url")+"=",MODEL_ALLOWED_HOSTS:"one.example"};
 execFileSync(python(root),["-m","alembic","upgrade","head"],{cwd:root,env,stdio:"pipe",windowsHide:true});
 const setup=name==="admin"?'from app.db import SessionLocal; from app.models import Admin':'from knowledge.db import SessionLocal; from knowledge.models import KnowledgeAdmin as Admin';
 execFileSync(python(root),["-c",setup+'; import os; from argon2 import PasswordHasher; db=SessionLocal(); db.add(Admin(username="smoke",password_hash=PasswordHasher().hash(os.environ["TEST_PASSWORD"]))); db.commit(); db.close()'],{cwd:root,env,stdio:"pipe",windowsHide:true});
 const child=spawn(python(root),["-m","uvicorn",module,"--host","127.0.0.1","--port",String(p)],{cwd:root,env,stdio:"ignore",windowsHide:true});children.push(child);
 for(let i=0;i<100;i++){try{if((await fetch(base+"/health/live")).ok)return {base,child};}catch{}await new Promise(r=>setTimeout(r,100));}
 throw Error(name+" did not start");
}
async function stop(child){if(child.exitCode!==null||child.signalCode!==null)return;const done=new Promise(r=>child.once("exit",r));if(process.platform==='win32'){try{execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{stdio:'ignore',windowsHide:true});}catch{child.kill();}}else{child.kill();}await done;}
let browser;
try{
 browser=await chromium.launch({channel:"msedge",headless:true});
 const errors=[];
 const page=await browser.newPage({viewport:{width:1440,height:1000}});page.setDefaultTimeout(10000);page.on("pageerror",e=>errors.push(e.message));
 const knowledge=await start(knowledgeRoot,"knowledge.main:app","knowledge");
 await page.goto(knowledge.base);await page.getByLabel("管理员账号").fill("smoke");await page.getByLabel("密码",{exact:true}).fill(password);await page.getByRole("button",{name:"登录知识管理",exact:true}).click();
 await page.getByRole("heading",{name:"新建知识库",exact:true}).waitFor();await page.getByLabel("名称",{exact:true}).fill("Independent smoke knowledge");
 const created=page.waitForResponse(r=>r.url().endsWith('/api/admin/v1/knowledge/knowledge-bases')&&r.request().method()==='POST');
 await page.getByRole("button",{name:"创建知识库",exact:true}).click();assert.equal((await created).status(),201);
 assert.equal(await page.getByRole("button",{name:"模型接入",exact:true}).count(),0);
 await page.screenshot({path:join(directory,"knowledge.png"),fullPage:true});
 await stop(knowledge.child);
 const platform=await start(adminRoot,"app.main:app","admin");
 await page.goto(platform.base);await page.getByLabel("管理员账号").fill("smoke");await page.getByLabel("密码",{exact:true}).fill(password);await page.getByRole("button",{name:"登录",exact:true}).click();
 await page.getByRole("heading",{name:"新增供应商",exact:true}).waitFor();
 await page.getByLabel("名称",{exact:true}).fill("Synthetic supplier");await page.getByLabel("API base URL（HTTPS）").fill("https://one.example/v1");await page.getByLabel("上游 API Key",{exact:true}).fill("synthetic-only-not-live");await page.getByRole("button",{name:"保存",exact:true}).click();
 await page.getByRole("heading",{name:"Synthetic supplier · 启用",exact:true}).waitFor();
 await page.getByRole("button",{name:"模型路由",exact:true}).click();await page.getByLabel("对外模型名",{exact:true}).fill("smoke-model");await page.getByRole("combobox").selectOption({label:"Synthetic supplier"});await page.getByLabel("上游真实模型名",{exact:true}).fill("upstream-smoke");await page.getByRole("button",{name:"保存路由",exact:true}).click();await page.getByRole("heading",{name:"smoke-model · 启用",exact:true}).waitFor();
 await page.getByRole("button",{name:"用户模型 Key",exact:true}).click();await page.getByLabel("用户/客户标识").fill("smoke-customer");await page.getByRole("listbox").selectOption("smoke-model");await page.getByRole("button",{name:"生成 Key",exact:true}).click();await page.getByRole("button",{name:"隐藏",exact:true}).waitFor();await page.getByRole("button",{name:"隐藏",exact:true}).click();
 await page.getByRole("button",{name:"调用监控",exact:true}).click();await page.getByRole("heading",{name:"当前页统计（最多 100 条）",exact:true}).waitFor();
 await page.screenshot({path:join(directory,"admin.png"),fullPage:true});
 assert.deepEqual(errors,[]);
 await stop(platform.child);
 console.log("PASS: standalone knowledge login/create-base with Admin absent; Admin login/provider/route/model-key/monitor with knowledge stopped; no browser errors.");
 console.log("Synthetic SQLite databases and screenshots retained at: "+directory);
}finally{if(browser)await browser.close();for(const child of children)await stop(child);}
