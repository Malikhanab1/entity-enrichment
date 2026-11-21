export interface IngestRequestBody {
  title: string;
  text: string;
  source: string;
  posted_by: string;
  user: string;
  group: string;
  posted_at: string;
}

/** The three ways a lead can be routed. Precedence when several are found: call > dm > mail */
export type LeadChannel = "call" | "dm" | "mail";

/** Roles a sales-team member can have. Maps 1:1 to LeadChannel. */
export type SalesRole = "caller" | "DM" | "email";

export const CHANNEL_TO_ROLE: Record<LeadChannel, SalesRole> = {
  call: "caller",
  dm: "DM",
  mail: "email",
};

export interface SalesTeamMember {
  name: string;
  contactNumber: string;
  role: SalesRole;
}

export interface ExtractedContact {
  channel: LeadChannel;
  value: string; // phone number, "dm", or email address
  candidates: {
    phones: string[];
    emails: string[];
    dmSignal: boolean;
  };
}

export interface IntentClassification {
  isLead: boolean;
  reason: string;
}

export interface AssignedLead {
  member: SalesTeamMember;
  channel: LeadChannel;
  contact: ExtractedContact;
}

export interface IngestResult {
  status: "discarded" | "duplicate" | "assigned";
  reason?: string;
  leadId?: string;
  assignedTo?: {
    name: string;
    role: SalesRole;
    contactNumber: string;
  };
}
