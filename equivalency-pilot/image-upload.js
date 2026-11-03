/* Classic-script API: CourseImageUpload.isImage/isPdf(file), inspectImage(bytes),
 * fitDimensions(width,height), jpegToPdf(jpeg,width,height),
 * prepare(file,{maxOutputBytes}) -> {bytes,width,height,originalWidth,originalHeight,format}.
 * Canvas re-encoding strips source metadata; no text is inferred or generated. */
(function (global) {
  'use strict';
  const MAX_INPUT_BYTES=12*1024*1024, MAX_OUTPUT_BYTES=4*1024*1024, MAX_PIXELS=32*1000*1000, MAX_LONG_SIDE=3200, JPEG_QUALITY=.94;
  const imageTypes=new Set(['image/jpeg','image/png','image/webp']);
  const isImage=file=>imageTypes.has((file.type||'').toLowerCase())||/\.(?:jpe?g|png|webp)$/i.test(file.name||'');
  const isPdf=file=>(file.type||'').toLowerCase()==='application/pdf'||/\.pdf$/i.test(file.name||'');
  const fail=message=>{throw new Error(message);};
  const invalid=()=>fail('تعذر قراءة الصورة. استخدم صورة JPG أو PNG أو WebP سليمة.');
  const ascii=value=>Uint8Array.from(value,c=>c.charCodeAt(0));
  const tag=(b,i,n)=>String.fromCharCode(...b.subarray(i,i+n));
  const be16=(b,i)=>(b[i]<<8)|b[i+1];
  const be32=(b,i)=>new DataView(b.buffer,b.byteOffset+i,4).getUint32(0,false);
  const le24=(b,i)=>b[i]|(b[i+1]<<8)|(b[i+2]<<16);
  function dimensions(width,height) {
    if(!Number.isInteger(width)||!Number.isInteger(height)||width<1||height<1)invalid();
    if(width*height>MAX_PIXELS)fail('دقة الصورة تتجاوز 32 ميغابكسل. اختر صورة لجدول المفردات فقط.');
    return {width,height};
  }
  function inspectImage(input) {
    const b=input instanceof Uint8Array?input:new Uint8Array(input);
    if(b.length>MAX_INPUT_BYTES)fail('الحد الأقصى للصورة 12 ميغابايت.');
    let size,format,bitDepth,components;
    if(b.length>=33&&tag(b,0,8)==='\x89PNG\r\n\x1a\n') {
      if(be32(b,8)!==13||tag(b,12,4)!=='IHDR')invalid();
      size=dimensions(be32(b,16),be32(b,20));format='png';
      for(let i=8;i+12<=b.length;) {
        const length=be32(b,i);if(length>b.length-i-12)invalid();
        if(tag(b,i+4,4)==='acTL'&&length>=8&&be32(b,i+8)>1)fail('استخدم صورة ثابتة واحدة لجدول المفردات.');
        i+=length+12;
      }
    } else if(b.length>=4&&b[0]===255&&b[1]===216) {
      format='jpeg';let i=2;
      while(i<b.length) {
        if(b[i++]!==255)invalid();while(i<b.length&&b[i]===255)i++;
        const marker=b[i++];if(marker===217||marker===218)break;
        if(marker===216||(marker>=208&&marker<=215)||marker===1)continue;
        if(i+2>b.length)invalid();const length=be16(b,i);if(length<2||i+length>b.length)invalid();
        if([192,193,194,195,197,198,199,201,202,203,205,206,207].includes(marker)) {
          if(length<8)invalid();size=dimensions(be16(b,i+5),be16(b,i+3));bitDepth=b[i+2];components=b[i+7];break;
        }
        i+=length;
      }
      if(!size)invalid();
    } else if(b.length>=25&&tag(b,0,4)==='RIFF'&&tag(b,8,4)==='WEBP') {
      format='webp';const kind=tag(b,12,4);
      if(kind==='VP8X'&&b.length>=30) {
        if(b[20]&2)fail('استخدم صورة ثابتة واحدة لجدول المفردات.');
        size=dimensions(1+le24(b,24),1+le24(b,27));
      } else if(kind==='VP8 '&&b.length>=30&&tag(b,23,3)==='\x9d\x01\x2a') {
        size=dimensions((b[26]|b[27]<<8)&16383,(b[28]|b[29]<<8)&16383);
      } else if(kind==='VP8L'&&b[20]===47) {
        size=dimensions(1+(b[21]|(b[22]&63)<<8),1+((b[22]>>6)|(b[23]<<2)|((b[24]&15)<<10)));
      } else invalid();
    } else invalid();
    return {...size,format,...(format==='jpeg'?{bitDepth,components}:{})};
  }
  function fitDimensions(width,height) {
    dimensions(width,height);
    const scale=Math.min(1,MAX_LONG_SIDE/Math.max(width,height));
    return {width:Math.max(1,Math.round(width*scale)),height:Math.max(1,Math.round(height*scale))};
  }
  function jpegToPdf(input,width,height) {
    const jpeg=input instanceof Uint8Array?input:new Uint8Array(input),info=inspectImage(jpeg);
    if(info.format!=='jpeg'||info.width!==width||info.height!==height||info.bitDepth!==8||info.components!==3||jpeg.at(-2)!==255||jpeg.at(-1)!==217)invalid();
    const parts=[],offsets=[0];let length=0;
    const append=bytes=>{parts.push(bytes);length+=bytes.length;};
    const text=value=>append(ascii(value));
    const object=(id,body)=>{offsets[id]=length;text(id+' 0 obj\n');body();text('\nendobj\n');};
    text('%PDF-1.4\n%\xe2\xe3\xcf\xd3\n');
    const scale=842/Math.max(width,height),pw=+(width*scale).toFixed(3),ph=+(height*scale).toFixed(3);
    object(1,()=>text('<< /Type /Catalog /Pages 2 0 R >>'));
    object(2,()=>text('<< /Type /Pages /Kids [3 0 R] /Count 1 >>'));
    object(3,()=>text('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 '+pw+' '+ph+'] /Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>'));
    object(4,()=>{text('<< /Type /XObject /Subtype /Image /Width '+width+' /Height '+height+' /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length '+jpeg.length+' >>\nstream\n');append(jpeg);text('\nendstream');});
    const content=ascii('q\n'+pw+' 0 0 '+ph+' 0 0 cm\n/Im0 Do\nQ\n');
    object(5,()=>{text('<< /Length '+content.length+' >>\nstream\n');append(content);text('endstream');});
    const xref=length;text('xref\n0 6\n0000000000 65535 f \n');
    for(let i=1;i<=5;i++)text(String(offsets[i]).padStart(10,'0')+' 00000 n \n');
    text('trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+xref+'\n%%EOF\n');
    const pdf=new Uint8Array(length);let cursor=0;for(const bytes of parts){pdf.set(bytes,cursor);cursor+=bytes.length;}return pdf;
  }
  async function decode(file) {
    // Modern decoders apply EXIF orientation before exposing bitmap dimensions.
    if(typeof global.createImageBitmap==='function')return global.createImageBitmap(file,{imageOrientation:'from-image'});
    // A data URL also works with the existing self/data-only image CSP.
    const url=await new Promise((resolve,reject)=>{const reader=new global.FileReader();reader.onload=()=>resolve(reader.result);reader.onerror=reject;reader.readAsDataURL(file);});
    return new Promise((resolve,reject)=>{const img=new global.Image();img.onload=()=>resolve(img);img.onerror=reject;img.src=url;});
  }
  async function prepare(file,{maxOutputBytes=4*1024*1024}={}) {
    if(!file||!Number.isFinite(file.size)||file.size<1)invalid();
    if(file.size>MAX_INPUT_BYTES)fail('الحد الأقصى للصورة 12 ميغابايت.');
    if(!Number.isSafeInteger(maxOutputBytes)||maxOutputBytes<1)fail('تعذر تحديد حد رفع الملف.');
    maxOutputBytes=Math.min(maxOutputBytes,MAX_OUTPUT_BYTES);
    const info=inspectImage(new Uint8Array(await file.arrayBuffer()));let image,canvas;
    try {
      try {image=await decode(file);}catch{invalid();}
      const originalWidth=image.naturalWidth||image.width,originalHeight=image.naturalHeight||image.height;
      const size=fitDimensions(originalWidth,originalHeight);canvas=global.document.createElement('canvas');canvas.width=size.width;canvas.height=size.height;
      const context=canvas.getContext('2d',{alpha:false,colorSpace:'srgb'});if(!context)fail('تعذر تجهيز الصورة في هذا المتصفح.');
      context.fillStyle='#ffffff';context.fillRect(0,0,size.width,size.height);context.imageSmoothingEnabled=true;context.imageSmoothingQuality='high';context.drawImage(image,0,0,size.width,size.height);
      const blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',JPEG_QUALITY));if(!blob)invalid();
      const jpeg=new Uint8Array(await blob.arrayBuffer()),bytes=jpegToPdf(jpeg,size.width,size.height);
      // Do not keep reducing quality or resolution to force a large image under the limit.
      if(bytes.length>maxOutputBytes)fail('الصورة كبيرة بعد تهيئتها. اختر لقطة أوضح لجدول المفردات فقط، أو ارفع توصيف PDF.');
      return {bytes,...size,originalWidth,originalHeight,format:info.format};
    } finally {image?.close?.();if(canvas){canvas.width=0;canvas.height=0;}}
  }
  global.CourseImageUpload=Object.freeze({isImage,isPdf,inspectImage,fitDimensions,jpegToPdf,prepare,MAX_INPUT_BYTES,MAX_OUTPUT_BYTES,MAX_PIXELS,MAX_LONG_SIDE,JPEG_QUALITY});
})(globalThis);
