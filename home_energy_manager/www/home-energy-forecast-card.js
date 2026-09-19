/* Home Energy Manager forecast card — install as a Lovelace JavaScript resource. */
class HomeEnergyForecastCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode:"open"});
    this.config={title:"Home Energy Forecast",rows:16,entity:"sensor.home_energy_forecast",
      edit_entity:"text.home_energy_manager_tariff_edit",
      result_entity:"sensor.home_energy_manager_tariff_edit_result"};
    this.pending=null;
    this._lastRenderKey=null;
  }
  static getConfigForm() {return {schema:[
    {name:"entity",required:true,selector:{entity:{domain:"sensor"}}},
    {name:"title",selector:{text:{}}},
    {name:"rows",selector:{number:{min:1,max:96,step:1,mode:"box"}}},
    {name:"edit_entity",selector:{entity:{domain:"text"}}},
    {name:"result_entity",selector:{entity:{domain:"sensor"}}}
  ]};}
  setConfig(config) {if(!config?.entity)throw Error("Please define an entity");this.config={...this.config,...config};this.render();}
  set hass(value) {this._hass=value;this.render();}
  connectedCallback(){this.render();}
  getCardSize(){return Math.max(4,Math.ceil((Number(this.config.rows)||16)/4));}
  fmt(value,digits=2){return Number.isFinite(Number(value))?Number(value).toFixed(digits):"—";}
  time(value){const d=new Date(value);return Number.isNaN(d.getTime())?"—":d.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",hour12:false});}
  escape(value){return String(value??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
  render(){
    if(!this._hass||!this.isConnected)return;
    const forecastState=this._hass.states[this.config.entity];
    const resultState=this._hass.states[this.config.result_entity];
    const renderKey=[forecastState?.last_updated,resultState?.last_updated,this.config.entity,this.config.rows,this.config.title].join("|");
    if(this._lastRenderKey===renderKey&&this.shadowRoot.querySelector("ha-card"))return;
    this._lastRenderKey=renderKey;
    const entity=this._hass.states[this.config.entity];
    const rows=entity?.attributes?.forecast?.slice(0,Math.max(1,Math.min(96,Number(this.config.rows)||16)))||[];
    const ack=this._hass.states[this.config.result_entity]?.attributes;
    if(this.pending&&ack?.id===this.pending.id){
      if(ack.status==="error"){this.pending.error=ack.error||"Edit rejected";this.pending.saving=false;}
      if(ack.status==="saved"){this.pending=null;}
    }
    const existing=this.shadowRoot.querySelector("dialog");
    const keep=existing?.open;
    const draft=keep?{value:existing.querySelector("input")?.value,start:existing.dataset.start,
      id:existing.dataset.id,error:existing.querySelector(".message")?.textContent}:null;
    this.shadowRoot.innerHTML=`<style>
      :host{display:block;font:13px var(--primary-font-family, sans-serif)}
      ha-card{overflow:hidden}header{display:flex;justify-content:space-between;gap:8px;padding:14px 16px}
      header strong{font-size:16px}.summary{color:var(--secondary-text-color);font-size:12px}
      .table{overflow-x:auto}.row{display:grid;grid-template-columns:1.05fr .5fr 1fr 1fr 1fr 1.3fr .9fr 1fr;
        align-items:center;border-top:1px solid var(--divider-color,#ddd);min-width:340px}
      .cell{padding:5px 3px;text-align:right;white-space:nowrap;min-width:0;overflow:hidden}
      .head{color:var(--secondary-text-color);font-weight:600}.time,.hp{text-align:center}
      button.price{font:inherit;color:var(--primary-color,#039be5);background:transparent;border:0;
        text-decoration:underline;cursor:pointer;padding:4px 2px;min-width:100%;text-align:right}
      button.price:disabled{opacity:.5;cursor:default}dialog{background:var(--card-background-color,#fff);
        color:var(--primary-text-color,#222);border:1px solid var(--divider-color,#ddd);border-radius:12px;
        padding:20px;max-width:min(90vw,380px);box-shadow:0 8px 30px #0003}
      dialog::backdrop{background:#0008}dialog label{display:block;margin:12px 0}
      dialog input{font:inherit;width:100%;box-sizing:border-box;padding:8px}
      dialog menu{display:flex;gap:8px;flex-wrap:wrap;padding:0;margin:16px 0 0}
      dialog button{padding:8px;border-radius:6px;cursor:pointer}
      .message{color:var(--error-color,#d33);min-height:1em}
      @container (max-width:520px){:host{font-size:11px}.cell{padding:3px 1px}}
    </style><ha-card><header><strong class="title"></strong><span class="summary"></span></header>
    <div class="table"><div class="row head">
    ${["Time","HP","Load","PV","Price","SoC","Cost","Total"].map(t=>`<div class="cell">${t}</div>`).join("")}</div>
    <div class="rows"></div></div></ha-card>
    <dialog><form method="dialog"><strong>Edit import price</strong><p class="period"></p>
    <label>Price (p/kWh)<input type="number" min="0" step="any" required></label>
    <div class="message" role="alert"></div><menu><button type="button" class="restore">Restore supplier price</button>
    <button type="button" class="cancel">Cancel</button><button type="submit" class="save">Save</button></menu></form></dialog>`;
    this.shadowRoot.querySelector(".title").textContent=this.config.title;
    this.shadowRoot.querySelector(".summary").textContent=
      Number.isFinite(Number(entity?.attributes?.overnight_start_soc))?
      `Overnight ${Math.round(Number(entity.attributes.overnight_start_soc))}%`:"";
    let running=Number(entity?.attributes?.today?.actual?.cost_p)||0;
    const body=this.shadowRoot.querySelector(".rows");
    if(!rows.length)body.textContent=entity?"No forecast data":"Forecast entity not found";
    for(const slot of rows){
      running+=Number(slot.cost_p)||0;
      const div=document.createElement("div");div.className="row";
      const hp=Number(slot.dhw_kwh)>0?"💧":Number(slot.ch_kwh)>0?"♨️":"";
      const values=[this.time(slot.start),hp,this.fmt(slot.load_kwh),this.fmt(slot.pv_kwh),
        null,`${this.fmt(slot.soc,0)}%`,`${this.fmt(slot.cost_p,0)}p`,`${this.fmt(running,0)}p`];
      values.forEach((value,index)=>{
        const cell=document.createElement("div");cell.className="cell"+(index===0?" time":index===1?" hp":"");
        if(index===4){
          const btn=document.createElement("button");btn.className="price";btn.type="button";
          btn.textContent=slot.import_rate_p==null?"—":`${this.fmt(slot.import_rate_p)}p`;
          btn.disabled=!slot.start||slot.import_rate_p==null;
          btn.title="Edit import price";btn.addEventListener("click",()=>this.openEditor(slot));
          cell.append(btn);
        }else cell.textContent=value;
        div.append(cell);
      });body.append(div);
    }
    const dialog=this.shadowRoot.querySelector("dialog");
    dialog.querySelector(".cancel").onclick=()=>dialog.close();
    dialog.querySelector(".save").onclick=e=>{e.preventDefault();this.save(false);};
    dialog.querySelector(".restore").onclick=()=>this.save(true);
    if(keep&&draft){dialog.dataset.start=draft.start;dialog.dataset.id=draft.id;
      dialog.querySelector("input").value=draft.value;dialog.querySelector(".message").textContent=draft.error;
      dialog.querySelector(".period").textContent=new Date(draft.start).toLocaleString();
      dialog.showModal();}
    if(this.pending&&dialog.open){dialog.querySelector(".message").textContent=
      this.pending.error||(this.pending.saving?"Saving…":"");
      dialog.querySelectorAll("button,input").forEach(el=>{el.disabled=this.pending.saving;});}
  }
  openEditor(slot){
    const dialog=this.shadowRoot.querySelector("dialog");dialog.dataset.start=slot.start;
    dialog.querySelector(".period").textContent=new Date(slot.start).toLocaleString();
    dialog.querySelector("input").value=slot.import_rate_p;
    dialog.querySelector(".message").textContent="";dialog.showModal();
  }
  async save(restore){
    const dialog=this.shadowRoot.querySelector("dialog");
    const input=dialog.querySelector("input");const price=restore?null:Number(input.value);
    if(!restore&&(!input.value.trim()||!Number.isFinite(price)||price<0)){
      dialog.querySelector(".message").textContent="Enter a valid non-negative price";return;}
    const id=Date.now().toString(36)+"-"+Math.random().toString(36).slice(2);
    this.pending={id,saving:true,error:null};
    this._lastRenderKey=null;
    dialog.querySelector(".message").textContent="Saving…";
    dialog.querySelectorAll("button,input").forEach(el=>el.disabled=true);
    try{
      await this._hass.callService("text","set_value",{entity_id:this.config.edit_entity,
        value:JSON.stringify({id,start:dialog.dataset.start,rate_p:price})});
      // The add-on acknowledgement, not service completion, confirms persistence.
    }catch(error){this.pending.saving=false;this.pending.error=String(error);
      dialog.querySelector(".message").textContent=this.pending.error;
      dialog.querySelectorAll("button,input").forEach(el=>el.disabled=false);}
  }
}
if(!customElements.get("home-energy-forecast-card"))
  customElements.define("home-energy-forecast-card",HomeEnergyForecastCard);
window.customCards=window.customCards||[];
if(!window.customCards.some(c=>c.type==="home-energy-forecast-card"))
  window.customCards.push({type:"home-energy-forecast-card",name:"Home Energy Forecast",
    description:"Half-hour home energy forecast with editable import prices"});
