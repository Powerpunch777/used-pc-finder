package local.usedpc.bridge;
import android.service.quicksettings.TileService;
import android.service.quicksettings.Tile;
import android.content.Intent;
public final class BridgeTileService extends TileService {
    public void onStartListening(){update();}
    public void onClick(){
        if(isLocked()) {unlockAndRun(()->toggle());return;}
        toggle();
    }
    private void toggle(){
        BridgeService s=BridgeService.instance;
        if(s==null)startActivityAndCollapse(new Intent(this,MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        else if(s.isRunning())s.stopDiagnostics();else s.startDiagnostics();
        update();
    }
    private void update(){Tile t=getQsTile();if(t!=null){BridgeService s=BridgeService.instance;
        t.setState(s!=null&&s.isRunning()?Tile.STATE_ACTIVE:Tile.STATE_INACTIVE);t.updateTile();}}
}
