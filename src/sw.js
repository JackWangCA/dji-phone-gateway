/* No offline page cache: SMS and login responses always come from the gateway. */
self.addEventListener('install',event=>event.waitUntil(self.skipWaiting()));
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('push',event=>{
  let payload={};
  try{payload=event.data?.json()||{}}catch(_){}
  event.waitUntil(self.registration.showNotification(payload.title||'SMS Forwarder',{
    body:payload.body||'You have a new message.',
    icon:'/apple-touch-icon.png?v=2',badge:'/apple-touch-icon.png?v=2',
    tag:payload.tag||'sms',data:{url:payload.url||'/#conversations',peer:payload.peer||''}
  }));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    const target=new URL(event.notification.data?.url||'/#conversations',self.location.origin);
    if(target.origin!==self.location.origin)return;
    const peer=event.notification.data?.peer||'';
    if(peer)target.hash='conversations?peer='+encodeURIComponent(peer);
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of windows){
      if(new URL(client.url).origin===target.origin){await client.navigate(target.href);return client.focus()}
    }
    return self.clients.openWindow(target.href);
  })());
});
