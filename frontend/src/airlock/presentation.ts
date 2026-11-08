type ControlRoomState = {
  connected: boolean;
  hasStation: boolean;
  armed: boolean;
  lock?: { status?: string; simulated?: boolean };
  incident?: { ruleId?: string };
};

/** Display only: action authorization and incident decisions remain with the server. */
export function controlRoomPresentation({ connected, hasStation, armed, lock, incident }: ControlRoomState): { title: string; status: string } {
  if (!connected) return { title: "Console disconnected", status: "Disconnected" };

  let title: string;
  let status: string;
  if (lock) {
    if (lock.status === "confirmed") {
      status = lock.simulated ? "Synthetic lock confirmation" : "Screen lock confirmed";
    } else {
      status = lock.simulated ? "Synthetic lock request committed" : "Screen lock requested";
    }
    title = status;
  } else if (incident?.ruleId === "owner_panic") {
    title = "Owner security alert";
    status = "Panic requested";
  } else if (incident) {
    title = "Activity needs your review";
    status = "Review required";
  } else if (armed) {
    title = "Your workstation is armed";
    status = "Armed";
  } else {
    title = "Your workstation is disarmed";
    status = "Disarmed";
  }

  return { title: hasStation ? title : "Your desk. Your boundary.", status };
}
