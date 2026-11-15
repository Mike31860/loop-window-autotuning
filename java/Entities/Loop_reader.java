package Entities;

public class Loop_reader {

    private String procedureName;
    private String loopId;
    private String desiredLoopOrder;

    public Loop_reader(String loopId, String procedureName, String desiredLoopOrder) {
        this.loopId = loopId;
        this.procedureName = procedureName;
        this.desiredLoopOrder = desiredLoopOrder;
    }
    public String getLoopId() {
        return loopId;
    }
    public String getProcedureName() {
        return procedureName;
    }
    public void setProcedureName(String procedureName) {
        this.procedureName = procedureName;
    }
    public void setLoopId(String loopId) {
        this.loopId = loopId;
    }
   
    public String getDesiredLoopOrder() {
        return desiredLoopOrder;
    }
    
}
