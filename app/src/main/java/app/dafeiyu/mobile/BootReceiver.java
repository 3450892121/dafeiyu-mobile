package app.dafeiyu.mobile;
import android.content.*;

public final class BootReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context,Intent intent){
        if(Intent.ACTION_BOOT_COMPLETED.equals(intent.getAction()) && context.getSharedPreferences("runtime",0).getBoolean("keep_running",false))
            EngineService.send(context,EngineService.OPEN);
    }
}
