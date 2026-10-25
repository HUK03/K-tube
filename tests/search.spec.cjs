const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const http = require('node:http');
const source = fs.readFileSync(path.join(__dirname,'..','k_content_scout_dashboard.html'));
(async () => {
 const server = http.createServer((req,res)=>{res.setHeader('Content-Type','text/html; charset=utf-8');res.end(source)});
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 const html = `http://127.0.0.1:${server.address().port}/`;
 const browser = await chromium.launch({headless:true, ...(require('node:fs').existsSync('/usr/bin/chromium') ? {executablePath:'/usr/bin/chromium'} : {}), args:['--no-sandbox']});
 let passed=0;
 async function test(name, options, check) {
  const page=await browser.newPage();const calls=[], errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(u.origin===new URL(html).origin) return route.continue();
   if(u.hostname==='127.0.0.1' && u.port==='5000' && options.downloadServer) {
    if(u.pathname==='/api/health') return route.fulfill({contentType:'application/json',body:JSON.stringify({status:'ok',video_download:options.downloadServer!=='old',ffmpeg:true})});
    if(u.pathname==='/api/download') {
     calls.push(u);
     assert.deepEqual(route.request().postDataJSON(),{videoId:'abc123def45'});
     if(options.downloadServer==='error') return route.fulfill({status:502,contentType:'application/json',body:JSON.stringify({error:'Video unavailable'})});
     return route.fulfill({contentType:'video/mp4',headers:{'Content-Disposition':'attachment; filename=youtube-abc123def45.mp4','Access-Control-Expose-Headers':'Content-Disposition'},body:Buffer.from('test video bytes')});
    }
   }
   if(u.hostname!=='www.googleapis.com') return route.abort();
   calls.push(u);
   const endpoint=u.pathname.split('/').pop();
   if(endpoint==='search' && options.quotaAtSearch && calls.filter(u=>u.pathname.endsWith('/search')).length>=options.quotaAtSearch) return route.fulfill({status:429,contentType:'application/json',body:JSON.stringify({error:{message: "Quota exceeded for Search Queries per day",errors:[{reason:'rateLimitExceeded'}]}})});
   if(options.error===endpoint) return route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({error:{message:'Quota exceeded',errors:[{reason:'quotaExceeded'}]}})});
   const videos=options.videos || [{id:'korean',title:'한국 음식 해외반응',views:1000}];
   let data;
   if(endpoint==='search') data={items:options.empty?[]:videos.map(v=>({id:{videoId:v.id}})),...(options.pages&&!u.searchParams.has('pageToken')?{nextPageToken:'NEXT'}:{})};
   if(endpoint==='search' && options.paginateFixtures) {
    const offset=Number(u.searchParams.get('pageToken')||0);
    data={items:videos.slice(offset,offset+50).map(v=>({id:{videoId:v.id}})),...(offset+50<videos.length?{nextPageToken:String(offset+50)}:{})};
   }
   if(endpoint==='videos') data={items:videos.filter(v=>u.searchParams.get('id').split(',').includes(v.id)).map(v=>({id:v.id,snippet:{title:v.title,channelId:'ch',channelTitle:'한국 채널',publishedAt:new Date().toISOString(),defaultAudioLanguage:'ko'},statistics:{viewCount:String(v.views)}}))};
   if(endpoint==='channels') data={items:options.missingChannel?[]:[{id:'ch',snippet:{country:'KR'},statistics:options.hidden?{hiddenSubscriberCount:true}:{subscriberCount:'100000'}}]};
   await route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
  });
  await page.goto(html);await page.waitForFunction(()=>document.getElementById('videoCardsContainer').children.length>0);
  await page.fill('#apiKeyInput','test-placeholder');await page.fill('#searchQueryInput','한국 음식 해외반응');
  try {await check(page,calls);assert.deepEqual(errors,[]);passed++;console.log('PASS',name);}
  finally {await page.close();}
 }
 async function search(page){await page.click('#executeSearchBtn');await page.waitForFunction(()=>!document.getElementById('executeSearchBtn').disabled);}
 await test('Korean/KR videos with low views remain visible and receive grade',{},async p=>{
  await search(p);assert.equal(await p.evaluate(()=>state.filteredVideos.length),1);
  assert.equal(await p.evaluate(()=>state.filteredVideos[0].country),'KR');
  assert.equal(await p.evaluate(()=>state.filteredVideos[0].grade),'S');
  assert.match(await p.locator('#videoCardsContainer').innerText(),/S 등급 · 1위/);
 });
 await test('Two search orders, pagination, no language exclusion, deduplication',{pages:true},async(p,calls)=>{
  await p.selectOption('#searchOrdersSelect','both');await p.selectOption('#searchPagesSelect','3');
  await search(p);const q=calls.filter(u=>u.pathname.endsWith('/search'));
  assert.equal(q.length,4);assert.deepEqual([...new Set(q.map(u=>u.searchParams.get('order')))],['relevance','viewCount']);
  assert.ok(q.every(u=>u.searchParams.get('q')==='한국 음식 해외반응'&&u.searchParams.get('maxResults')==='50'));
  assert.equal(await p.evaluate(()=>state.allVideos.length),1);
 });
 for(const endpoint of ['search','videos','channels'])await test(endpoint+' errors are persistent, no false success',{error:endpoint},async p=>{
  await search(p);assert.match(await p.locator('#searchStatus').innerText(),new RegExp('검색 실패: '+endpoint));
  assert.match(await p.locator('#searchStatus').innerText(),/quotaExceeded/);
  assert.equal(await p.evaluate(()=>state.allVideos.length),0);
 });
 await test('Empty result clears demo and subtitle studio',{empty:true},async p=>{
  await search(p);assert.equal(await p.evaluate(()=>state.allVideos.length),0);
  assert.equal(await p.evaluate(()=>state.selectedVideo),null);
  assert.equal(await p.locator('#transcriptFullTextarea').inputValue(),'');
  assert.match(await p.locator('#searchStatus').innerText(),/수집 후보 0개/);
 });
 for(const option of ['hidden','missingChannel']) await test(option+' subscriber counts stay unknown and visible',{[option]:true},async p=>{
  await search(p);assert.equal(await p.evaluate(()=>state.filteredVideos[0].subscriberCount),null);
  assert.equal(await p.evaluate(()=>state.filteredVideos[0].viralRatio),null);
  assert.match(await p.locator('#videoCardsContainer').innerText(),/확인 불가/);
 });
 await test('New search resets bookmark tab and quick filter',{},async p=>{
  await p.click('[data-tab="bookmarks"]');await p.fill('#quickTextFilterInput','no match');
  await search(p);assert.equal(await p.evaluate(()=>state.currentTab),'all');assert.equal(await p.locator('#quickTextFilterInput').inputValue(),'');
  assert.equal(await p.evaluate(()=>state.filteredVideos.length),1);
 });
 await test('S/A/B sorting prioritizes query relevance above unrelated popularity',{videos:[{id:'b',title:'Football championship',views:10000000},{id:'a',title:'한국 일상',views:5000},{id:'s',title:'한국 음식 해외반응',views:100}]},async p=>{
  await search(p);assert.deepEqual(await p.evaluate(()=>state.filteredVideos.map(v=>v.grade)),['S','A','B']);
  assert.deepEqual(await p.evaluate(()=>state.filteredVideos.map(v=>v.videoId)),['s','a','b']);
  await p.fill('#minViewsInput','99999999');assert.equal(await p.evaluate(()=>state.filteredVideos.length),3);
 });
 await test('Page limit reports additional results',{pages:true},async(p,calls)=>{
  await p.selectOption('#searchPagesSelect','1');await search(p);
  assert.equal(calls.filter(u=>u.pathname.endsWith('/search')).length,1);
  assert.match(await p.locator('#searchStatus').innerText(),/추가 결과/);
 });
 await test('No API key does not pretend to search demo',{},async p=>{
  await p.fill('#apiKeyInput','');await search(p);assert.equal(await p.evaluate(()=>state.allVideos.length),0);
  assert.match(await p.locator('#searchStatus').innerText(),/API 키를 입력/);
 });
 await test('Manual query is preserved when adding keyword chip',{},async p=>{
  await p.fill('#searchQueryInput','내 검색 목적');
  await p.locator('#keywordChipsContainer button').first().click();
  assert.ok((await p.locator('#searchQueryInput').inputValue()).includes('내 검색 목적'));
 });
 await test('120 unique candidates survive real-sized pagination and 50-ID detail batches',{paginateFixtures:true,videos:Array.from({length:120},(_,i)=>({id:'v'+i,title:'한국 음식 해외반응 '+i,views:i}))},async(p,calls)=>{
  await p.selectOption('#searchPagesSelect','3');
  await search(p);assert.equal(await p.evaluate(()=>state.filteredVideos.length),120);
  assert.equal(calls.filter(u=>u.pathname.endsWith('/videos')).length,3);
  assert.ok(calls.filter(u=>u.pathname.endsWith('/videos')).every(u=>u.searchParams.get('id').split(',').length<=50));
 });
 await test('More than 10 keywords is rejected explicitly without silent truncation',{},async(p,calls)=>{
  await p.fill('#searchQueryInput',Array.from({length:11},(_,i)=>'keyword'+i).join('|'));await search(p);
  assert.equal(calls.length,0);assert.match(await p.locator('#toastContainer').innerText(),/1~10개/);
 });
 await test('Download button saves returned file with server filename',{downloadServer:'ok',videos:[{id:'abc123def45',title:'한국 음식 해외반응',views:100}]},async p=>{
  await search(p);
  const downloaded=p.waitForEvent('download');
  await p.locator('.download-btn').click();
  const file=await downloaded;assert.equal(file.suggestedFilename(),'youtube-abc123def45.mp4');
  assert.equal(fs.readFileSync(await file.path(),'utf8'),'test video bytes');
  await p.waitForFunction(()=>state.downloadBusy===false);
  assert.match(await p.locator('#downloadStatus').innerText(),/저장 요청/);
 });
 await test('Sample downloads explain that real search is required',{},async p=>{
  await p.locator('.download-btn').first().click();
  assert.match(await p.locator('#downloadStatus').innerText(),/샘플 영상은 다운로드할 수 없습니다/);
 });
 for(const [kind,message] of [['old',/최신 server.py/],['offline',/python server.py/],['error',/Video unavailable/]]) {
  await test('Download '+kind+' has a persistent error and restores buttons',{downloadServer:kind==='offline'?null:kind,videos:[{id:'abc123def45',title:'한국 음식 해외반응',views:100}]},async p=>{
   await search(p);await p.locator('.download-btn').click();
   await p.waitForFunction(()=>state.downloadBusy===false);
   assert.match(await p.locator('#downloadStatus').innerText(),message);
   assert.equal(await p.locator('.download-btn').isEnabled(),true);
  });
 }
 await test('Default three-keyword search uses at most three search requests',{pages:true},async(p,calls)=>{
  await p.fill('#searchQueryInput','한국 해외반응 | Korea reaction | Korean food reaction');
  assert.match(await p.locator('#searchBudgetStatus').innerText(),/최대 3회/);
  await search(p);assert.equal(calls.filter(u=>u.pathname.endsWith('/search')).length,3);
 });
 await test('Identical queries reuse cache, including after page reload',{},async(p,calls)=>{
  await search(p);const count=calls.length;
  await search(p);assert.equal(calls.length,count);
  assert.match(await p.locator('#searchStatus').innerText(),/추가 API 요청 없음/);
  await p.reload();await p.fill('#searchQueryInput','한국 음식 해외반응');
  await search(p);assert.equal(calls.length,count);
  assert.equal(await p.evaluate(()=>state.filteredVideos.length),1);
 });
 await test('Force refresh explicitly bypasses cache',{},async(p,calls)=>{
  await search(p);const count=calls.length;
  await p.check('#forceRefreshCheckbox');await search(p);assert.ok(calls.length>count);
 });
 await test('Expired cache triggers fresh requests',{},async(p,calls)=>{
  await search(p);const count=calls.length;
  await p.evaluate(()=>{state.lastSuccessfulSearch.savedAt-=3600001;localStorage.setItem('kscout_search_cache_v1',JSON.stringify(state.lastSuccessfulSearch));});
  await search(p);assert.ok(calls.length>count);
 });
 await test('Daily 429 is actionable and never automatically retried',{quotaAtSearch:1},async(p,calls)=>{
  await search(p);assert.equal(calls.filter(u=>u.pathname.endsWith('/search')).length,1);
  assert.match(await p.locator('#searchStatus').innerText(),/하루 검색 한도/);
  assert.match(await p.locator('#searchStatus').innerText(),/한도 갱신 또는 증액/);
 });
 await test('Quota failure preserves previous successful query and ranking',{quotaAtSearch:2},async p=>{
  await search(p);const videos=await p.evaluate(()=>state.filteredVideos.map(v=>[v.videoId,v.grade]));
  await p.fill('#searchQueryInput','different query');await search(p);
  assert.deepEqual(await p.evaluate(()=>state.filteredVideos.map(v=>[v.videoId,v.grade])),videos);
  assert.match(await p.locator('#searchStatus').innerText(),/이전 성공 결과를 유지/);
  assert.equal(await p.evaluate(()=>state.rankingQueries[0]),'한국 음식 해외반응');
 });
 await test('Page quota failure displays collected candidates with partial-result notice',{quotaAtSearch:2,pages:true},async(p,calls)=>{
  await p.selectOption('#searchPagesSelect','3');await search(p);
  assert.equal(calls.filter(u=>u.pathname.endsWith('/search')).length,2);
  assert.equal(await p.evaluate(()=>state.filteredVideos.length),1);
  assert.match(await p.locator('#searchStatus').innerText(),/일부 결과만 표시/);
  assert.match(await p.locator('#searchStatus').innerText(),/rateLimitExceeded/);
 });
 await browser.close();server.close();console.log(`${passed} browser regression scenarios passed (mock YouTube API).`);
})().catch(e=>{console.error(e);process.exit(1)});
