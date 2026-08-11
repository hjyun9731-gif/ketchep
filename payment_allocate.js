
(function(){
  const pickers=[...document.querySelectorAll('[data-member-picker]')];
  pickers.forEach(function(picker){
    const input=picker.querySelector('.member-picker-input');
    const clear=picker.querySelector('.member-picker-clear');
    const results=picker.querySelector('.member-picker-results');
    const hidden=picker.parentElement.querySelector('input[type="hidden"][name$="-member"]');
    const url=picker.dataset.searchUrl;
    let timer=null, controller=null, activeIndex=-1;

    function closeResults(){results.hidden=true;results.innerHTML='';activeIndex=-1;}
    function setSelected(item){
      hidden.value=item.id;
      input.value=item.label;
      input.dataset.selectedLabel=item.label;
      closeResults();
      input.focus();
    }
    function render(items){
      if(!items.length){results.innerHTML='<div class="member-picker-empty">검색 결과가 없습니다.</div>';results.hidden=false;return;}
      results.innerHTML=items.map((item,idx)=>{
        const meta=[item.vehicle,item.region,item.management_no ? '관리 '+item.management_no : '',item.phone,item.status].filter(Boolean).join(' · ');
        return `<button type="button" class="member-picker-option" data-index="${idx}" data-id="${item.id}"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(meta)}</span></button>`;
      }).join('');
      results.hidden=false;
      results.querySelectorAll('.member-picker-option').forEach((button,idx)=>{
        button.addEventListener('mousedown',function(e){e.preventDefault();setSelected(items[idx]);});
      });
    }
    function escapeHtml(value){return String(value||'').replace(/[&<>'"]/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));}
    async function search(){
      const q=input.value.trim();
      if(input.dataset.selectedLabel && q===input.dataset.selectedLabel){closeResults();return;}
      hidden.value='';
      if(q.length<1){closeResults();return;}
      if(controller) controller.abort(); controller=new AbortController();
      try{
        const response=await fetch(url+'?q='+encodeURIComponent(q),{headers:{'X-Requested-With':'XMLHttpRequest'},signal:controller.signal});
        if(!response.ok) throw new Error('lookup failed');
        const data=await response.json(); render(data.results||[]);
      }catch(error){if(error.name!=='AbortError'){results.innerHTML='<div class="member-picker-empty">검색 중 오류가 발생했습니다.</div>';results.hidden=false;}}
    }
    input.addEventListener('input',function(){delete input.dataset.selectedLabel;clearTimeout(timer);timer=setTimeout(search,160);});
    input.addEventListener('focus',function(){if(input.value.trim() && !hidden.value) search();});
    input.addEventListener('keydown',function(e){
      const options=[...results.querySelectorAll('.member-picker-option')];
      if(results.hidden || !options.length) return;
      if(e.key==='ArrowDown'){e.preventDefault();activeIndex=Math.min(options.length-1,activeIndex+1);}
      else if(e.key==='ArrowUp'){e.preventDefault();activeIndex=Math.max(0,activeIndex-1);}
      else if(e.key==='Enter' && activeIndex>=0){e.preventDefault();options[activeIndex].dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));return;}
      else if(e.key==='Escape'){closeResults();return;} else return;
      options.forEach((node,idx)=>node.classList.toggle('active',idx===activeIndex));
      options[activeIndex].scrollIntoView({block:'nearest'});
    });
    clear.addEventListener('click',function(){hidden.value='';input.value='';delete input.dataset.selectedLabel;closeResults();input.focus();});
    document.addEventListener('click',function(e){if(!picker.contains(e.target)) closeResults();});
    if(hidden.value && input.value){input.dataset.selectedLabel=input.value;}
  });
})();
