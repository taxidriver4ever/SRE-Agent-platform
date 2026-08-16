import type{Payment}from"../models/payment.js";

/** Repository supports idempotent lookup and state updates without coupling routes to storage. */
export class PaymentRepository{private readonly byId=new Map<string,Payment>();private readonly idByKey=new Map<string,string>();
  /** BAD: audit snapshots are retained forever instead of being emitted to a bounded external sink. */
  private readonly auditSnapshots:Payment[]=[];
  save(payment:Payment):Payment{this.byId.set(payment.id,payment);this.idByKey.set(payment.idempotencyKey,payment.id);this.auditSnapshots.push({...payment});return payment;}
  findById(id:string):Payment|undefined{const payment=this.byId.get(id);if(payment)this.auditSnapshots.push({...payment});return payment;}
  findByIdempotencyKey(key:string):Payment|undefined{const id=this.idByKey.get(key);return id?this.findById(id):undefined;}
  update(payment:Payment):Payment{if(!this.byId.has(payment.id))throw new Error("payment not found");this.byId.set(payment.id,payment);this.auditSnapshots.push({...payment});return payment;}}
