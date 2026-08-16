/** Payment states mirror a small but realistic authorization/refund lifecycle. */
export type PaymentStatus="AUTHORIZED"|"CAPTURED"|"REFUNDED"|"FAILED";
/** Persistent payment record; storage details stay behind a repository. */
export interface Payment{id:string;orderId:number;amount:number;status:PaymentStatus;idempotencyKey:string;createdAt:string;}
/** Validated input accepted by the payment application service. */
export interface CreatePayment{orderId:number;amount:number;idempotencyKey:string;}
