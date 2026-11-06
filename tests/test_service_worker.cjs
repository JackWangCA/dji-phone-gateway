const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/sw.js'), 'utf8');

async function check(navigator, payload) {
  const events = {}, notifications = [];
  const self = {navigator, addEventListener:(name, callback)=>events[name]=callback,
    registration:{showNotification:async (...args)=>notifications.push(args)}};
  vm.runInNewContext(source, {self, URL, Promise});
  let completion;
  events.push({data:{json:()=>payload}, waitUntil:promise=>completion=promise});
  await completion;
  assert.equal(notifications.length, 1, 'Every push still displays a notification');
}

(async()=>{
  const badges=[];
  await check({setAppBadge:async count=>badges.push(count)}, {badge_count:3});
  await check({setAppBadge:async count=>badges.push(count)}, {badge_count:0});
  assert.deepEqual(badges,[3,0]);
  await check({setAppBadge:async ()=>{throw new Error('Permission denied')}}, {badge_count:1});
  await check(undefined, {badge_count:1});
  await check({setAppBadge:async ()=>{throw new Error('Must not be called')}}, {badge_count:-1});
  console.log('PASS: push badge count, clearing, permission errors, unsupported devices, and visible notifications');
})().catch(error=>{console.error(error);process.exitCode=1});
