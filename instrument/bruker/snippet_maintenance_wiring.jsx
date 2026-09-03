/* ===========================================================================
 * ONE-LINE WIRING for the Maintenance tab
 *
 * In stan/dashboard/public/index.html, find `function MaintenanceTab()`
 * (~line 4575). Its return is roughly:
 *
 *     return (
 *       <div>
 *         <div className="card"> ...calendar... </div>
 *         ...
 *         <div className="card">
 *           <h3 style={{margin:0}}>Maintenance Log</h3>
 *           <MaintenanceEventsTable events={events} />
 *         </div>
 *       </div>            <-- closing div of the return
 *     );
 *
 * Add <BrukerAcquisitionPanel /> as the LAST child, just before that final
 * closing </div>:
 * ======================================================================== */

//        </div>   {/* end Maintenance Log card */}

          <BrukerAcquisitionPanel />

//      </div>     {/* end MaintenanceTab return */}
