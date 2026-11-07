import {sqliteTable,text,integer,primaryKey,index} from 'drizzle-orm/sqlite-core';
export const documents=sqliteTable('documents',{
 userId:text('user_id').notNull(),kind:text('kind').notNull(),id:text('id').notNull(),body:text('body').notNull(),revision:integer('revision').notNull().default(1),updatedAt:text('updated_at').notNull()
},t=>[primaryKey({columns:[t.userId,t.kind,t.id]}),index('idx_documents_user_kind_updated').on(t.userId,t.kind,t.updatedAt)]);
export const files=sqliteTable('files',{
 id:text('id').primaryKey(),userId:text('user_id').notNull(),name:text('name').notNull(),mime:text('mime').notNull(),size:integer('size').notNull(),createdAt:text('created_at').notNull()
},t=>[index('idx_files_user_id').on(t.userId)]);
export const subscriptions=sqliteTable('subscriptions',{
 userId:text('user_id').primaryKey(),customerKey:text('customer_key').notNull().unique(),billingKey:text('billing_key'),paidUntil:text('paid_until'),status:text('status').notNull().default('PENDING'),cancelAtPeriodEnd:integer('cancel_at_period_end').notNull().default(0),updatedAt:text('updated_at').notNull()
});
export const billingOrders=sqliteTable('billing_orders',{
 orderId:text('order_id').primaryKey(),userId:text('user_id').notNull(),amount:integer('amount').notNull(),status:text('status').notNull(),paymentKey:text('payment_key'),periodStart:text('period_start'),periodEnd:text('period_end'),createdAt:text('created_at').notNull(),updatedAt:text('updated_at').notNull()
},t=>[index('idx_billing_orders_user_created').on(t.userId,t.createdAt)]);
export const authSessions=sqliteTable('auth_sessions',{
 tokenHash:text('token_hash').primaryKey(),userId:text('user_id').notNull(),googleSub:text('google_sub').notNull(),email:text('email').notNull(),expiresAt:text('expires_at').notNull(),createdAt:text('created_at').notNull()
},t=>[index('idx_auth_sessions_user').on(t.userId),index('idx_auth_sessions_expiry').on(t.expiresAt)]);
