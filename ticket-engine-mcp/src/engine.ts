export interface Ticket {
    id?: number;
    file_path: string;
    number: number;
    area: string;
    type: string;
    status: string;
    title?: string;
    objective?: string;
    created_at?: string;
    updated_at?: string;
}

export interface TicketEngine {
    ticketDir: string;
    indexPath: string;
    
    create(params: { area?: string; type?: string; title: string; objective?: string }): Ticket;
    list(area?: string, status?: string): Ticket[];
    update(number: number, patch: Partial<Omit<Ticket, 'id' | 'file_path' | 'number'>>): Ticket | undefined;
    archive(number: number): Ticket | undefined;
    validate(): { synced: boolean; missing_files?: string[]; extra_files?: string[] };
}
